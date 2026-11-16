from __future__ import annotations

import json
import asyncio

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, Field
from pydantic import StrictBool
from sse_starlette.sse import EventSourceResponse

from server.core.agent.acceptance import AcceptanceCheck
from server.core.chain.types import TurnRequest, StreamEvent
from server.core.reminders.parse import parse_reminder_line
from server.core.reminders.quiet import in_quiet_hours, next_quiet_end_utc

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ApprovalDecisionBody(BaseModel):
    session_id: str
    approve: StrictBool


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(approval_id: str, body: ApprovalDecisionBody, request: Request) -> dict:
    # This initial control surface is restricted to trusted local clients.
    if request.headers.get("origin") or not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "approval decisions require a local non-browser client")
    coordinator = request.app.state.velora.router.approvals
    if coordinator is None:
        raise HTTPException(409, "approval coordinator unavailable")
    try:
        await coordinator.decide(approval_id, session_id=body.session_id, approve=body.approve)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"approval_id": approval_id, "decision": "approved" if body.approve else "denied"}


class ChatBody(BaseModel):
    session_id: str
    text: str = Field(min_length=1)
    acceptance: list[AcceptanceCheck] = Field(default_factory=list, max_length=20)


class CancelBody(BaseModel):
    session_id: str


@router.get("/tasks")
async def recent_tasks(session_id: str, request: Request) -> dict:
    state = request.app.state.velora
    if await state.store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    return {"tasks": await state.tasks.recent(session_id)}


@router.get("/tasks/{task_id}")
async def read_task(task_id: str, request: Request) -> dict:
    result = await request.app.state.velora.tasks.read(task_id)
    if result is None:
        raise HTTPException(404, "task not found")
    return result


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after: int = Query(0, ge=0)):
    service = request.app.state.velora.tasks
    result = await service.read(task_id)
    if result is None:
        raise HTTPException(404, "task not found")
    if after > result["cursor"]:
        raise HTTPException(409, "event cursor exceeds task history")
    return EventSourceResponse(service.watch(task_id, after))


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    service = request.app.state.velora.tasks
    if await service.read(task_id) is None:
        raise HTTPException(404, "task not found")
    return {"cancel_requested": service.cancel(task_id)}


@router.post("/cancel")
async def cancel_chat(body: CancelBody, request: Request) -> dict:
    state = request.app.state.velora
    if await state.store.get_session(body.session_id) is None:
        raise HTTPException(404, "session not found")
    return {"cancelled": state.router.cancel(body.session_id)}


class ResumeBody(BaseModel):
    run_id: str
    text: str = ""


@router.post("/resume/stream")
async def resume_stream(body: ResumeBody, request: Request):
    state = request.app.state.velora
    store = state.router.run_store
    if store is None:
        raise HTTPException(409, "runtime recovery unavailable")
    source = await store.read(body.run_id)
    if source is None:
        raise HTTPException(404, "run checkpoint not found")
    session = await state.store.get_session(source["session_id"])
    if session is None:
        raise HTTPException(404, "session not found")
    if state.router.is_running(source["session_id"]):
        raise HTTPException(409, "source session is still executing")
    if (state.settings.llm_model or session.model) != source["model"]:
        raise HTTPException(409, "resume requires the original model")
    recovery = await store.recovery_status(body.run_id)
    if not recovery or not recovery["recoverable"]:
        raise HTTPException(409, "checkpoint is not at a proven safe boundary")
    if recovery["kind"] == "waiting_for_input" and not body.text.strip():
        raise HTTPException(409, "waiting run requires feedback")
    try:
        identifier = await state.tasks.submit(TurnRequest(source["session_id"], body.text), resume_run_id=body.run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return EventSourceResponse(state.tasks.watch(identifier))


@router.post("/resume")
async def resume_chat(body: ResumeBody, request: Request) -> dict:
    state = request.app.state.velora
    if state.router.run_store is None:
        raise HTTPException(409, "Runtime recovery is not enabled")
    source = await state.router.run_store.read(body.run_id)
    if source is None:
        raise HTTPException(404, "run checkpoint not found")
    try:
        identifier = await state.tasks.submit(TurnRequest(source["session_id"], body.text), resume_run_id=body.run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    task = state.tasks.active.get(identifier)
    if task is not None:
        await asyncio.shield(task)
    record = await state.tasks.read(identifier)
    result = record.get("result") or {}
    outcome = result.get("result") or {"status": record["status"]}
    return {"task_id": identifier, "run_id": outcome.get("run_id"), "status": outcome["status"],
            "assistant_text": result.get("assistant_text", ""), "stop_reason": result.get("stop_reason"), "result": outcome}


@router.post("/stream")
async def chat_stream(body: ChatBody, request: Request) -> EventSourceResponse:
    state = request.app.state.velora
    session = await state.store.get_session(body.session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    # Short-circuit: 请提醒：事项 | ISO8601
    parsed = None if body.acceptance else parse_reminder_line(body.text)
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
        async def reminder_task(emit):
            rem = await state.store.create_reminder(session_id=body.session_id, note=parsed.note, due_at=due)
            reply = f"已设提醒：{parsed.note}（{rem.due_at}）"
            await state.store.save_exchange(body.session_id, body.text, reply)
            await emit(StreamEvent("token", {"text": reply}))
            await emit(StreamEvent("message", {"role": "assistant", "content": reply}))
            await emit(StreamEvent("reminder_created", {"id": rem.id, "due_at": rem.due_at, "note": rem.note}))
            await emit(StreamEvent("turn_end", {"session_id": body.session_id, "assistant_text": reply,
                "stop_reason": None, "result": {"status": "completed", "reason": None, "message": "",
                    "run_id": None, "step": 0, "verification": {"status": "not_requested"}}}))
        try:
            identifier = await state.tasks.submit(TurnRequest(body.session_id, body.text), runner=reminder_task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return EventSourceResponse(state.tasks.watch(identifier))

    try:
        task_id = await state.tasks.submit(TurnRequest(session_id=body.session_id, user_text=body.text,
            client_meta={"acceptance": [check.model_dump() for check in body.acceptance]}))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return EventSourceResponse(state.tasks.watch(task_id))
