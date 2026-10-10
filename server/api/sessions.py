from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server.core.computer.workspace import WorkspaceError, resolve_in_workspace
from server.core.persona.default import BUILTIN_PERSONA_ID
from server.core.persona.resolve import resolve_persona
from server.core.persona.style import clamp_warmth

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionBody(BaseModel):
    model: str | None = None
    tts_enabled: bool = False
    warmth: int | None = None
    persona_id: str | None = None


class PatchSessionBody(BaseModel):
    style_knobs: dict[str, Any] | None = None
    warmth: int | None = None
    tts_enabled: bool | None = None
    model: str | None = None
    persona_id: str | None = None
    greeting_index: int | None = None


class SessionOut(BaseModel):
    id: str
    persona_id: str
    persona_name: str
    memory_space_uid: str
    model: str
    tts_enabled: bool
    style_knobs: dict[str, Any] = Field(default_factory=dict)
    warmth: int = 35
    greeting_index: int = 0
    created_at: str
    updated_at: str


class AttachmentOut(BaseModel):
    path: str
    name: str
    bytes: int
    kind: str = "file"


class TurnOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    source: str = "chat"
    attachments: list[AttachmentOut] = Field(default_factory=list)


async def _session_out(row, store) -> SessionOut:
    knobs = dict(row.style_knobs or {})
    warmth = clamp_warmth(knobs.get("warmth", 35))
    knobs["warmth"] = warmth
    try:
        greeting_index = int(knobs.get("greeting_index") or 0)
    except (TypeError, ValueError):
        greeting_index = 0
    persona = await resolve_persona(store, row.persona_id)
    return SessionOut(
        id=row.id,
        persona_id=row.persona_id,
        persona_name=persona.name,
        memory_space_uid=row.memory_space_uid,
        model=row.model,
        tts_enabled=row.tts_enabled,
        style_knobs=knobs,
        warmth=warmth,
        greeting_index=max(0, greeting_index),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn_out(t) -> TurnOut:
    atts: list[AttachmentOut] = []
    for raw in getattr(t, "attachments", None) or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip()
        name = str(raw.get("name") or "").strip() or path
        if not path:
            continue
        try:
            size = int(raw.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        kind = str(raw.get("kind") or "file")
        atts.append(AttachmentOut(path=path, name=name, bytes=size, kind=kind))
    return TurnOut(
        id=t.id,
        role=t.role,
        content=t.content,
        created_at=t.created_at,
        source=getattr(t, "source", None) or "chat",
        attachments=atts,
    )


async def _ensure_persona_id(state, persona_id: str | None) -> str:
    pid = (persona_id or "").strip() or state.settings.default_persona_id
    if pid == BUILTIN_PERSONA_ID:
        return BUILTIN_PERSONA_ID
    row = await state.store.get_persona(pid)
    if row is None:
        raise HTTPException(400, f"unknown persona_id: {pid}")
    return pid


@router.post("", response_model=SessionOut)
async def create_session(
    request: Request, body: CreateSessionBody | None = None
) -> SessionOut:
    state = request.app.state.velora
    body = body or CreateSessionBody()
    warmth = clamp_warmth(
        body.warmth if body.warmth is not None else state.settings.default_warmth
    )
    persona_id = await _ensure_persona_id(
        state, body.persona_id or state.settings.default_persona_id
    )
    row = await state.store.create_session(
        persona_id=persona_id,
        memory_space_uid=state.settings.memory_space_uid,
        model=body.model or state.settings.llm_model,
        tts_enabled=body.tts_enabled,
        style_knobs={"warmth": warmth, "greeting_index": 0},
    )
    try:
        await state.memory.ensure_space(row.memory_space_uid)
    except Exception:
        pass
    return await _session_out(row, state.store)


@router.get("", response_model=list[SessionOut])
async def list_sessions(request: Request) -> list[SessionOut]:
    state = request.app.state.velora
    rows = await state.store.list_sessions()
    return [await _session_out(r, state.store) for r in rows]


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: str, request: Request) -> SessionOut:
    state = request.app.state.velora
    row = await state.store.get_session(session_id)
    if row is None:
        raise HTTPException(404, "session not found")
    return await _session_out(row, state.store)


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
    if body.greeting_index is not None:
        knobs["greeting_index"] = max(0, int(body.greeting_index))
    persona_id = None
    if body.persona_id is not None:
        persona_id = await _ensure_persona_id(state, body.persona_id)
    row = await state.store.update_session(
        session_id,
        style_knobs=knobs or None,
        tts_enabled=body.tts_enabled,
        model=body.model,
        persona_id=persona_id,
    )
    if row is None:
        raise HTTPException(404, "session not found")
    return await _session_out(row, state.store)


@router.get("/{session_id}/turns", response_model=list[TurnOut])
async def list_turns(session_id: str, request: Request) -> list[TurnOut]:
    state = request.app.state.velora
    if await state.store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    turns = await state.store.list_turns(
        session_id, limit=state.settings.history_max_messages
    )
    return [_turn_out(t) for t in turns]


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@router.get("/{session_id}/workspace/{file_path:path}")
async def get_workspace_file(
    session_id: str, file_path: str, request: Request
) -> FileResponse:
    state = request.app.state.velora
    if await state.store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    rel = unquote(file_path or "").strip().lstrip("/")
    if not rel:
        raise HTTPException(400, "path required")
    try:
        target = resolve_in_workspace(
            state.settings.workspaces_dir,
            session_id,
            rel,
            allow_abs=False,
        )
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not target.is_file():
        raise HTTPException(404, "file not found")
    inline = target.suffix.lower() in _IMAGE_EXTS
    return FileResponse(
        path=target,
        filename=target.name,
        content_disposition_type="inline" if inline else "attachment",
    )
