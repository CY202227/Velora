"""Authenticated execution control; approval-requiring submissions stay interactive."""
import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/execution", tags=["execution"])


def environment(request):
    return request.app.state.velora.execution


@router.get("/capabilities")
async def capabilities(request: Request):
    return environment(request).capabilities()


class SubmitBody(BaseModel):
    session_id: str
    operation: Literal["run_shell", "run_python", "read_file", "write_file", "list_dir"]
    arguments: dict
    execution_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


@router.post("")
async def submit(body: SubmitBody, request: Request):
    state = request.app.state.velora
    if request.headers.get("origin") or not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "direct submissions require a local non-browser client")
    if state.settings.execution_require_approval or body.arguments.get("permissions"):
        raise HTTPException(409, "use an interactive task for approval-requiring execution")
    if len(json.dumps(body.arguments)) > 16000:
        raise HTTPException(413, "split execution arguments into smaller operations")
    if await state.store.get_session(body.session_id) is None:
        raise HTTPException(404, "session not found")
    if state.router.is_running(body.session_id) or body.session_id in state.tasks.busy:
        raise HTTPException(409, "session has an active task")
    try:
        identifier = await state.execution.submit(body.session_id, body.operation, body.arguments, body.execution_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"execution_id": identifier}


@router.get("/{identifier}")
async def inspect(identifier: str, request: Request):
    record = await environment(request).store.read(identifier, include_result=True)
    if record is None:
        raise HTTPException(404, "execution not found")
    return record


@router.post("/{identifier}/cancel")
async def cancel(identifier: str, request: Request):
    if await environment(request).store.read(identifier) is None:
        raise HTTPException(404, "execution not found")
    return {"cancel_requested": environment(request).store.cancel(identifier), "effects_rolled_back": False}


@router.post("/{identifier}/reconcile")
async def reconcile(identifier: str, request: Request):
    await inspect(identifier, request)
    return await environment(request).store.reconcile(identifier)


@router.get("/{identifier}/artifacts")
async def artifacts(identifier: str, request: Request, channel: Literal["stdout", "stderr"] = "stdout", offset: int = Query(0, ge=0), limit: int = Query(12000, ge=1, le=16000)):
    record = await inspect(identifier, request)
    try:
        return await asyncio.to_thread(environment(request).output, identifier, record["session_id"], channel, offset, limit)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{identifier}/events")
async def events(identifier: str, request: Request, after: str = ""):
    await inspect(identifier, request)
    async def stream():
        cursor = after
        while True:
            record = await environment(request).store.read(identifier)
            if record["updated_at"] != cursor:
                cursor = record["updated_at"]
                yield {"event": "execution_state", "id": cursor, "data": json.dumps(record)}
            if record["status"] not in ("prepared", "running"):
                return
            await asyncio.sleep(.2)
    return EventSourceResponse(stream())
