"""Persona CRUD + chara_card_v2 import + avatar."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server.core.persona.chara_png import extract_chara_json_from_png
from server.core.persona.chara_v2 import (
    CharaCardError,
    compose_system_prompt,
    parse_chara_card_json,
    parse_mes_example,
)
from server.core.persona.default import (
    BUILTIN_PERSONA_ID,
    DEFAULT_PERSONA,
    Persona,
    greetings_for,
)
from server.core.persona.resolve import row_to_persona

router = APIRouter(prefix="/api/personas", tags=["personas"])

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class PersonaOut(BaseModel):
    id: str
    name: str
    system_prompt: str
    opener: str | None = None
    begin_dialogs: list[str] = Field(default_factory=list)
    style_defaults: dict[str, Any] = Field(default_factory=dict)
    tool_names: list[str] | None = None
    skill_names: list[str] | None = None
    source: str = "custom"
    description: str = ""
    personality: str = ""
    scenario: str = ""
    post_history_instructions: str = ""
    alternate_greetings: list[str] = Field(default_factory=list)
    greetings: list[str] = Field(default_factory=list)
    character_book: dict[str, Any] | None = None
    avatar_url: str | None = None
    has_avatar: bool = False
    import_meta: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    readonly: bool = False


class PersonaWrite(BaseModel):
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    system_prompt: str | None = None
    opener: str | None = None
    begin_dialogs: list[str] = Field(default_factory=list)
    mes_example: str | None = None
    post_history_instructions: str = ""
    alternate_greetings: list[str] = Field(default_factory=list)
    character_book: dict[str, Any] | None = None
    style_defaults: dict[str, Any] = Field(default_factory=dict)
    tool_names: list[str] | None = None
    skill_names: list[str] | None = None
    avatar_url: str | None = None


class AvatarUrlBody(BaseModel):
    avatar_url: str


def _avatars_dir(request: Request) -> Path:
    root = Path(request.app.state.velora.settings.workspaces_dir).parent
    d = root / "personas" / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _persona_out(p: Persona, *, created_at: str | None = None, updated_at: str | None = None) -> PersonaOut:
    has_avatar = bool(p.avatar_path) or bool(p.avatar_url)
    return PersonaOut(
        id=p.id,
        name=p.name,
        system_prompt=p.system_prompt,
        opener=p.opener,
        begin_dialogs=list(p.begin_dialogs or []),
        style_defaults=dict(p.style_defaults or {}),
        tool_names=p.tool_names,
        skill_names=p.skill_names,
        source=p.source,
        description=p.description or "",
        personality=p.personality or "",
        scenario=p.scenario or "",
        post_history_instructions=p.post_history_instructions or "",
        alternate_greetings=list(p.alternate_greetings or []),
        greetings=greetings_for(p),
        character_book=p.character_book,
        avatar_url=f"/api/personas/{p.id}/avatar" if has_avatar else None,
        has_avatar=has_avatar,
        import_meta=p.import_meta,
        created_at=created_at,
        updated_at=updated_at,
        readonly=p.source == "builtin" or p.id == BUILTIN_PERSONA_ID,
    )


def _builtin_out() -> PersonaOut:
    return _persona_out(DEFAULT_PERSONA, created_at=None, updated_at=None)


@router.get("", response_model=list[PersonaOut])
async def list_personas(request: Request) -> list[PersonaOut]:
    state = request.app.state.velora
    out = [_builtin_out()]
    for row in await state.store.list_personas():
        if row.id == BUILTIN_PERSONA_ID:
            continue
        out.append(
            _persona_out(
                row_to_persona(row),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return out


@router.get("/{persona_id}", response_model=PersonaOut)
async def get_persona(persona_id: str, request: Request) -> PersonaOut:
    if persona_id == BUILTIN_PERSONA_ID:
        return _builtin_out()
    state = request.app.state.velora
    row = await state.store.get_persona(persona_id)
    if row is None:
        raise HTTPException(404, "persona not found")
    return _persona_out(
        row_to_persona(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _from_write(body: PersonaWrite, *, persona_id: str, source: str) -> Persona:
    begin = list(body.begin_dialogs or [])
    if body.mes_example and not begin:
        begin = parse_mes_example(body.mes_example)
    system = (body.system_prompt or "").strip()
    if not system:
        system = compose_system_prompt(
            description=body.description,
            personality=body.personality,
            scenario=body.scenario,
            card_system_prompt="",
            char_name=body.name,
        )
    return Persona(
        id=persona_id,
        name=body.name.strip() or "Unnamed",
        system_prompt=system,
        style_defaults=dict(body.style_defaults or {}),
        opener=(body.opener or None),
        begin_dialogs=begin,
        tool_names=body.tool_names,
        skill_names=body.skill_names,
        source=source,
        description=body.description or "",
        personality=body.personality or "",
        scenario=body.scenario or "",
        post_history_instructions=body.post_history_instructions or "",
        alternate_greetings=list(body.alternate_greetings or []),
        character_book=body.character_book,
        avatar_url=(body.avatar_url or None),
        avatar_path=None,
        import_meta={"mes_example_raw": body.mes_example}
        if body.mes_example
        else None,
    )


@router.post("", response_model=PersonaOut)
async def create_persona(body: PersonaWrite, request: Request) -> PersonaOut:
    state = request.app.state.velora
    pid = str(uuid.uuid4())
    persona = _from_write(body, persona_id=pid, source="custom")
    row = await state.store.upsert_persona(persona)
    if body.avatar_url:
        await _download_avatar(request, pid, body.avatar_url)
        row = await state.store.get_persona(pid) or row
    return _persona_out(
        row_to_persona(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.put("/{persona_id}", response_model=PersonaOut)
async def update_persona(
    persona_id: str, body: PersonaWrite, request: Request
) -> PersonaOut:
    if persona_id == BUILTIN_PERSONA_ID:
        raise HTTPException(400, "builtin persona is read-only")
    state = request.app.state.velora
    existing = await state.store.get_persona(persona_id)
    if existing is None:
        raise HTTPException(404, "persona not found")
    persona = _from_write(body, persona_id=persona_id, source=existing.source)
    persona.avatar_path = existing.avatar_path
    if body.avatar_url is not None:
        persona.avatar_url = body.avatar_url
    else:
        persona.avatar_url = existing.avatar_url
    row = await state.store.upsert_persona(persona)
    return _persona_out(
        row_to_persona(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.delete("/{persona_id}")
async def delete_persona(persona_id: str, request: Request) -> dict[str, str]:
    if persona_id == BUILTIN_PERSONA_ID:
        raise HTTPException(400, "builtin persona cannot be deleted")
    state = request.app.state.velora
    ok = await state.store.delete_persona(persona_id)
    if not ok:
        raise HTTPException(404, "persona not found")
    # Cleanup avatars
    for p in _avatars_dir(request).glob(f"{persona_id}.*"):
        try:
            p.unlink()
        except OSError:
            pass
    return {"status": "ok"}


@router.post("/import", response_model=PersonaOut)
async def import_persona(
    request: Request,
    file: UploadFile | None = File(None),
) -> PersonaOut:
    state = request.app.state.velora
    pid = str(uuid.uuid4())
    ctype_header = (request.headers.get("content-type") or "").lower()

    # JSON body (application/json)
    if "application/json" in ctype_header and file is None:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid JSON body") from exc
        try:
            persona = parse_chara_card_json(payload, persona_id=pid)
        except CharaCardError as exc:
            raise HTTPException(400, str(exc)) from exc
        row = await state.store.upsert_persona(persona)
        if persona.avatar_url:
            await _download_avatar(request, pid, persona.avatar_url)
            row = await state.store.get_persona(pid) or row
        return _persona_out(
            row_to_persona(row),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    if file is None:
        raise HTTPException(400, "file or JSON body required")

    raw = await file.read()
    filename = file.filename or ""
    content_type = file.content_type or ""
    lower = filename.lower()
    is_png = lower.endswith(".png") or "png" in content_type
    is_json = (
        lower.endswith(".json")
        or "json" in content_type
        or raw[:1] in (b"{", b"[")
    )

    try:
        if is_png:
            payload = extract_chara_json_from_png(raw)
            persona = parse_chara_card_json(payload, persona_id=pid)
            dest = _avatars_dir(request) / f"{pid}.png"
            dest.write_bytes(raw)
            persona.avatar_path = str(dest)
            persona.avatar_url = None
        elif is_json:
            payload = json.loads(raw.decode("utf-8"))
            persona = parse_chara_card_json(payload, persona_id=pid)
        else:
            raise HTTPException(400, "supported: .json or .png character card")
    except CharaCardError as exc:
        raise HTTPException(400, str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid JSON") from exc

    row = await state.store.upsert_persona(persona)
    if persona.avatar_url and not persona.avatar_path:
        await _download_avatar(request, pid, persona.avatar_url)
        row = await state.store.get_persona(pid) or row
    return _persona_out(
        row_to_persona(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _download_avatar(request: Request, persona_id: str, url: str) -> None:
    state = request.app.state.velora
    url = (url or "").strip()
    if not url.startswith("http"):
        return
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
            ctype = resp.headers.get("content-type", "")
    except Exception:
        return
    ext = ".png"
    if "jpeg" in ctype or "jpg" in ctype:
        ext = ".jpg"
    elif "webp" in ctype:
        ext = ".webp"
    elif "gif" in ctype:
        ext = ".gif"
    dest = _avatars_dir(request) / f"{persona_id}{ext}"
    dest.write_bytes(data)
    row = await state.store.get_persona(persona_id)
    if row is None:
        return
    p = row_to_persona(row)
    p.avatar_path = str(dest)
    p.avatar_url = url
    await state.store.upsert_persona(p)


@router.post("/{persona_id}/avatar", response_model=PersonaOut)
async def upload_avatar(
    persona_id: str,
    request: Request,
    file: UploadFile | None = File(None),
) -> PersonaOut:
    if persona_id == BUILTIN_PERSONA_ID:
        raise HTTPException(400, "builtin persona is read-only")
    state = request.app.state.velora
    row = await state.store.get_persona(persona_id)
    if row is None:
        raise HTTPException(404, "persona not found")

    if file is not None:
        raw = await file.read()
        name = (file.filename or "avatar.png").lower()
        ext = ".png"
        for e in _IMAGE_EXTS:
            if name.endswith(e):
                ext = e
                break
        dest = _avatars_dir(request) / f"{persona_id}{ext}"
        # remove old
        for p in _avatars_dir(request).glob(f"{persona_id}.*"):
            try:
                p.unlink()
            except OSError:
                pass
        dest.write_bytes(raw)
        persona = row_to_persona(row)
        persona.avatar_path = str(dest)
        row = await state.store.upsert_persona(persona)
        return _persona_out(
            row_to_persona(row),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    try:
        body = AvatarUrlBody.model_validate(await request.json())
    except Exception as exc:
        raise HTTPException(400, "file or {avatar_url} required") from exc
    await _download_avatar(request, persona_id, body.avatar_url)
    row = await state.store.get_persona(persona_id)
    if row is None:
        raise HTTPException(404, "persona not found")
    return _persona_out(
        row_to_persona(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/{persona_id}/avatar")
async def get_avatar(persona_id: str, request: Request):
    if persona_id == BUILTIN_PERSONA_ID:
        raise HTTPException(404, "no avatar")
    state = request.app.state.velora
    row = await state.store.get_persona(persona_id)
    if row is None:
        raise HTTPException(404, "persona not found")
    if row.avatar_path:
        path = Path(row.avatar_path)
        if path.is_file():
            return FileResponse(
                path,
                filename=path.name,
                content_disposition_type="inline",
            )
    # fallback glob
    matches = list(_avatars_dir(request).glob(f"{persona_id}.*"))
    if matches:
        path = matches[0]
        return FileResponse(
            path,
            filename=path.name,
            content_disposition_type="inline",
        )
    if row.avatar_url and row.avatar_url.startswith("http"):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(row.avatar_url)
    raise HTTPException(404, "no avatar")
