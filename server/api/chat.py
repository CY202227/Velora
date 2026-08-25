from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from server.core.chain.types import StreamEvent, TurnRequest
from server.core.reminders.parse import parse_reminder_line
from server.core.reminders.quiet import in_quiet_hours, next_quiet_end_utc

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatBody(BaseModel):
    session_id: str
    text: str = Field(min_length=1)


@router.post("/stream")
async def chat_stream(body: ChatBody, request: Request) -> EventSourceResponse:
    state = request.app.state.velora
    session = await state.store.get_session(body.session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    # Short-circuit: 请提醒：事项 | ISO8601
    parsed = parse_reminder_line(body.text)
    if parsed is not None:
        due = parsed.due_at
        s = state.settings
        if s.quiet_hours_enabled and in_quiet_hours(
            due,
            tz_name=s.user_timezone,
            start=s.quiet_hours_start,
            end=s.quiet_hours_end,
        ):
            due = next_quiet_end_utc(
                due,
                tz_name=s.user_timezone,
                start=s.quiet_hours_start,
                end=s.quiet_hours_end,
            )
        rem = await state.store.create_reminder(
            session_id=body.session_id,
            note=parsed.note,
            due_at=due,
        )
        await state.store.add_turn(body.session_id, "user", body.text)
        reply = f"已设提醒：{parsed.note}（{rem.due_at}）"
        await state.store.add_turn(body.session_id, "assistant", reply)

        async def reminder_events() -> AsyncIterator[dict]:
            yield {
                "event": "token",
                "data": json.dumps({"text": reply}, ensure_ascii=False),
            }
            yield {
                "event": "message",
                "data": json.dumps(
                    {"role": "assistant", "content": reply}, ensure_ascii=False
                ),
            }
            yield {
                "event": "reminder_created",
                "data": json.dumps(
                    {"id": rem.id, "due_at": rem.due_at, "note": rem.note},
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "turn_end",
                "data": json.dumps(
                    {"session_id": body.session_id, "stop_reason": "reminder_set"},
                    ensure_ascii=False,
                ),
            }

        return EventSourceResponse(reminder_events())

    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

    async def emit(event: StreamEvent) -> None:
        await queue.put(event)

    async def run_turn() -> None:
        try:
            await state.router.handle(
                TurnRequest(session_id=body.session_id, user_text=body.text),
                emit=emit,
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_turn())

    async def event_generator() -> AsyncIterator[dict]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                item = await queue.get()
                if item is None:
                    break
                yield {
                    "event": item.type,
                    "data": json.dumps(item.data, ensure_ascii=False),
                }
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(event_generator())
