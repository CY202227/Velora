from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.core.persona.default import DEFAULT_PERSONA
from server.core.persona.style import clamp_warmth

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionBody(BaseModel):
    model: str | None = None
    tts_enabled: bool = False
    warmth: int | None = None


class PatchSessionBody(BaseModel):
    style_knobs: dict[str, Any] | None = None
    warmth: int | None = None
    tts_enabled: bool | None = None
    model: str | None = None


class SessionOut(BaseModel):
    id: str
    persona_id: str
    persona_name: str
    memory_space_uid: str
    model: str
    tts_enabled: bool
    style_knobs: dict[str, Any] = Field(default_factory=dict)
    warmth: int = 35
    created_at: str
    updated_at: str


class TurnOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: str


def _session_out(row) -> SessionOut:
    knobs = dict(row.style_knobs or {})
    warmth = clamp_warmth(knobs.get("warmth", 35))
    knobs["warmth"] = warmth
    return SessionOut(
        id=row.id,
        persona_id=row.persona_id,
        persona_name=DEFAULT_PERSONA.name,
        memory_space_uid=row.memory_space_uid,
        model=row.model,
        tts_enabled=row.tts_enabled,
        style_knobs=knobs,
        warmth=warmth,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", response_model=SessionOut)
async def create_session(
    request: Request, body: CreateSessionBody | None = None
) -> SessionOut:
    state = request.app.state.velora
    body = body or CreateSessionBody()
    warmth = clamp_warmth(
        body.warmth if body.warmth is not None else state.settings.default_warmth
    )
    row = await state.store.create_session(
        persona_id=DEFAULT_PERSONA.id,
        memory_space_uid=state.settings.memory_space_uid,
        model=body.model or state.settings.llm_model,
        tts_enabled=body.tts_enabled,
        style_knobs={"warmth": warmth},
    )
    try:
        await state.memory.ensure_space(row.memory_space_uid)
    except Exception:
        pass
    return _session_out(row)


@router.get("", response_model=list[SessionOut])
async def list_sessions(request: Request) -> list[SessionOut]:
    state = request.app.state.velora
    rows = await state.store.list_sessions()
    return [_session_out(r) for r in rows]


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: str, request: Request) -> SessionOut:
    state = request.app.state.velora
    row = await state.store.get_session(session_id)
    if row is None:
        raise HTTPException(404, "session not found")
    return _session_out(row)


@router.patch("/{session_id}", response_model=SessionOut)
async def patch_session(
    session_id: str, body: PatchSessionBody, request: Request
) -> SessionOut:
    state = request.app.state.velora
    knobs = dict(body.style_knobs or {})
    if body.warmth is not None:
        knobs["warmth"] = clamp_warmth(body.warmth)
    elif "warmth" in knobs:
        knobs["warmth"] = clamp_warmth(knobs["warmth"])
    row = await state.store.update_session(
        session_id,
        style_knobs=knobs or None,
        tts_enabled=body.tts_enabled,
        model=body.model,
    )
    if row is None:
        raise HTTPException(404, "session not found")
    return _session_out(row)


@router.get("/{session_id}/turns", response_model=list[TurnOut])
async def list_turns(session_id: str, request: Request) -> list[TurnOut]:
    state = request.app.state.velora
    if await state.store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    turns = await state.store.list_turns(
        session_id, limit=state.settings.history_max_messages
    )
    return [
        TurnOut(id=t.id, role=t.role, content=t.content, created_at=t.created_at)
        for t in turns
    ]
