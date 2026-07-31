"""Debug status for the built-in desk."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/debug", tags=["debug"])


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
