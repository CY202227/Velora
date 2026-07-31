from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/memory", tags=["memory"])


class CorrectionBody(BaseModel):
    text: str = Field(min_length=1)


@router.get("/atoms")
async def list_atoms(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    kind: str | None = None,
) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    try:
        return await state.memory.list_atoms(
            uid, page=page, page_size=page_size, kind=kind
        )
    except Exception as exc:
        raise HTTPException(502, f"atom-memory error: {exc}") from exc


@router.get("/atoms/{key}")
async def get_atom(key: str, request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    try:
        return await state.memory.get_atom(uid, key)
    except Exception as exc:
        raise HTTPException(502, f"atom-memory error: {exc}") from exc


@router.post("/atoms/{key}/archive")
async def archive_atom(key: str, request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    try:
        return await state.memory.archive_atom(uid, key)
    except Exception as exc:
        raise HTTPException(502, f"atom-memory error: {exc}") from exc


@router.post("/corrections")
async def post_correction(body: CorrectionBody, request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    written = await state.memory.add_source(
        uid,
        kind="correction",
        content=body.text,
        salience=0.9,
        external_ref={"system": "velora", "via": "memory_panel"},
    )
    if written is None:
        raise HTTPException(502, "failed to write correction source")
    await state.consolidate_job.run_now(uid, trigger="correction")
    return {"ok": True, "source": written}


@router.post("/consolidate")
async def consolidate_now(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    result = await state.memory.consolidate(uid, trigger="manual")
    if result is None:
        raise HTTPException(502, "consolidate failed")
    return result
