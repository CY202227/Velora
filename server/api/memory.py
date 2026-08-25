from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.core.memory.labels import build_memory_summary

router = APIRouter(prefix="/api/memory", tags=["memory"])


class CorrectionBody(BaseModel):
    text: str = Field(min_length=1)


class ArchiveKindBody(BaseModel):
    kind: str = Field(min_length=1)


@router.get("/summary")
async def memory_summary(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    try:
        data = await state.memory.list_atoms(uid, page=1, page_size=100)
    except Exception as exc:
        raise HTTPException(502, f"atom-memory error: {exc}") from exc
    results = data.get("results") or []
    summary = build_memory_summary(results)
    # Prefer service count when available and larger
    count = data.get("count")
    if isinstance(count, int) and count > summary["total"]:
        # Rebuild headline with real total while keeping capped sections
        parts = [
            f"{s['label']} {s['count']}" for s in summary["sections"]
        ]
        if count == 0:
            summary["headline"] = "还没有记住什么。可以说「请记住…」。"
        else:
            summary["headline"] = f"已记住 {count} 条" + (
                f"：{' · '.join(parts)}" if parts else ""
            )
        summary["total"] = count
    return summary


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


@router.post("/atoms/archive-kind")
async def archive_kind(body: ArchiveKindBody, request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    uid = state.settings.memory_space_uid
    kind = body.kind.strip()
    try:
        data = await state.memory.list_atoms(
            uid, page=1, page_size=100, kind=kind
        )
    except Exception as exc:
        raise HTTPException(502, f"atom-memory error: {exc}") from exc

    archived = 0
    failed = 0
    errors: list[str] = []
    for atom in data.get("results") or []:
        key = atom.get("key")
        if not key:
            continue
        status = str(atom.get("status") or "active").lower()
        if status in ("archived", "deleted", "tombstone"):
            continue
        try:
            await state.memory.archive_atom(uid, str(key))
            archived += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{key}: {exc}")
    return {
        "ok": failed == 0,
        "kind": kind,
        "archived": archived,
        "failed": failed,
        "errors": errors[:10],
    }


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
