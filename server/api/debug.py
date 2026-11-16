"""Debug status for the built-in desk."""

from __future__ import annotations

from typing import Any
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from server.core.inference_log import recent

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/executions")
async def execution_records(request: Request, session_id: str | None = None) -> dict[str, Any]:
    store = request.app.state.velora.router.execution_store
    return {"executions": await store.recent(session_id=session_id) if store else []}


@router.get("/executions/{execution_id}")
async def execution_record(execution_id: str, request: Request) -> dict[str, Any]:
    store = request.app.state.velora.router.execution_store
    record = await store.read(execution_id) if store else None
    if record is None:
        raise HTTPException(404, "execution record not found")
    return record


@router.get("/execution")
async def execution_status(request: Request) -> dict[str, Any]:
    from server.core.execution.sandbox import sandbox_status
    return sandbox_status(request.app.state.velora.settings)


@router.get("/runtime/checkpoints")
async def runtime_checkpoints(request: Request) -> dict[str, Any]:
    store = request.app.state.velora.router.run_store
    return {"runs": await store.recent() if store is not None else []}


@router.get("/runtime/checkpoints/{run_id}")
async def runtime_checkpoint(run_id: str, request: Request) -> dict[str, Any]:
    store = request.app.state.velora.router.run_store
    data = await store.read(run_id) if store is not None else None
    if data is None:
        raise HTTPException(404, "run checkpoint not found")
    run = data["run"]
    return {
        "run_id": run_id, "session_id": data["session_id"],
        "revision": data["revision"], "phase": run["phase"],
        "outcome": run["outcome"], "step": run["step"],
        "replay_safe_boundary": data["replay_safe"],
        "recovery": await store.recovery_status(run_id),
        "tools": [{k: tool[k] for k in ("id", "name", "round", "status")} for tool in run["tool_executions"]],
    }


@router.get("/runtime")
async def runtime_status(request: Request) -> dict[str, Any]:
    return request.app.state.velora.router.runtime_status()


@router.get("/inference")
async def inference_log(request: Request) -> dict[str, Any]:
    return {
        "tts_configured": bool(request.app.state.velora.settings.resolve_vertin_script()),
        "events": list(recent),
    }


@router.get("/speech/{name}")
async def speech(name: str) -> FileResponse:
    try:
        valid = name == f"{UUID(hex=name.removesuffix('.wav')).hex}.wav"
    except ValueError:
        valid = False
    path = Path(__file__).resolve().parents[2] / "velora_data" / "speech" / name
    if not valid or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/wav")


@router.get("/status")
async def debug_status(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    s = state.settings
    mem_ok = False
    mem_detail: Any = None
    try:
        mem_detail = await state.memory.health()
        mem_ok = True
    except Exception as exc:
        mem_detail = str(exc)

    llm_models: Any = None
    llm_ok = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {}
            if s.llm_api_key:
                headers["Authorization"] = f"Bearer {s.llm_api_key}"
            resp = await client.get(f"{s.llm_base_url.rstrip('/')}/models", headers=headers)
            if resp.status_code < 400:
                llm_ok = True
                data = resp.json()
                ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                llm_models = ids[:20]
            else:
                llm_models = f"{resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        llm_models = str(exc)

    sessions = await state.store.list_sessions(limit=5)
    return {
        "velora": "ok",
        "llm": {
            "ok": llm_ok,
            "base_url": s.llm_base_url,
            "model": s.llm_model,
            "api_key_set": bool(s.llm_api_key),
            "models_sample": llm_models,
        },
        "memory": {
            "ok": mem_ok,
            "base_url": s.atom_memory_base_url,
            "space_uid": s.memory_space_uid,
            "detail": mem_detail,
        },
        "sessions_recent": [
            {"id": x.id, "updated_at": x.updated_at, "model": x.model} for x in sessions
        ],
    }
