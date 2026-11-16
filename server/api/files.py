"""Explicit local file-change inspection and restoration."""
import asyncio

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool
import json

from server.core.computer.workspace import workspace_root
from server.core.execution.snapshots import SnapshotError

router = APIRouter(prefix="/api/files/checkpoints", tags=["file checkpoints"])


def local(request):
    if request.headers.get("origin") or not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "file restoration requires a local non-browser client")


async def context(request, session_id):
    state = request.app.state.velora
    if await state.store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    if state.settings.execution_location != "local" or state.router.snapshots is None:
        raise HTTPException(409, "local file checkpoints unavailable")
    return state, state.router.snapshots, workspace_root(state.settings.workspaces_dir, session_id)


@router.get("")
async def recent(request: Request, session_id: str):
    local(request)
    _, snapshots, _ = await context(request, session_id)
    return {"checkpoints": await asyncio.to_thread(snapshots.recent, session_id)}


@router.get("/{identifier}/changes")
async def changes(identifier: str, request: Request, session_id: str):
    local(request)
    _, snapshots, root = await context(request, session_id)
    try:
        return await asyncio.to_thread(snapshots.preview, identifier, session_id, root)
    except (SnapshotError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


class RestoreBody(BaseModel):
    session_id: str
    confirm: StrictBool


@router.post("/{identifier}/restore")
async def restore(identifier: str, body: RestoreBody, request: Request):
    local(request)
    if not body.confirm:
        raise HTTPException(409, "explicit restoration confirmation required")
    state, snapshots, root = await context(request, body.session_id)
    if state.router.is_running(body.session_id):
        raise HTTPException(409, "finish or cancel the active task before restoring files")
    session_lock = await state.router.locks.acquire(body.session_id)
    async with session_lock, snapshots.lock(root):
        work = asyncio.create_task(asyncio.to_thread(snapshots.restore, identifier, body.session_id, root))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await asyncio.shield(work)
            raise
        except (SnapshotError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc


async def run_checkpoint(run_id, request, session_id):
    state, snapshots, root = await context(request, session_id)
    if state.router.is_running(session_id):
        raise HTTPException(409, "finish or cancel the active task before grouping file changes")
    if state.router.run_store is None:
        raise HTTPException(409, "run checkpoints unavailable")
    source = await state.router.run_store.read(run_id)
    if source is None or source["session_id"] != session_id:
        raise HTTPException(404, "run checkpoint not found in this session")
    identifiers = []
    for tool in source["run"]["tool_executions"]:
        if tool["name"] not in ("run_shell", "run_python", "write_file"):
            continue
        if tool["status"] != "returned":
            raise HTTPException(409, "run includes unresolved file operations")
        try:
            identifier = json.loads(tool["result"])["checkpoint_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(409, "run includes an operation without file checkpoint coverage") from exc
        identifiers.append(identifier)
    if not identifiers:
        raise HTTPException(409, "run has no covered file operations")
    try:
        identifier = await asyncio.to_thread(snapshots.combine, identifiers, run_id, session_id, root)
    except (SnapshotError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return identifier


@router.get("/runs/{run_id}/changes")
async def run_changes(run_id: str, request: Request, session_id: str):
    local(request)
    identifier = await run_checkpoint(run_id, request, session_id)
    return await changes(identifier, request, session_id)


@router.post("/runs/{run_id}/restore")
async def run_restore(run_id: str, body: RestoreBody, request: Request):
    local(request)
    if not body.confirm:
        raise HTTPException(409, "explicit restoration confirmation required")
    identifier = await run_checkpoint(run_id, request, body.session_id)
    return await restore(identifier, body, request)
