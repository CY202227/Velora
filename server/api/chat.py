from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from server.core.chain.types import StreamEvent, TurnRequest

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
