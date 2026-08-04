from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from server.core.persona.default import DEFAULT_PERSONA
from server.core.settings_store import save_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    llm_base_url: str
    llm_model: str
    llm_api_key_set: bool
    atom_memory_base_url: str
    memory_space_uid: str
    tts_enabled: bool
    show_memory_hints: bool
    default_warmth: int
    preset_id: str
    start_memory_sidecar: bool
    persona_name: str
    persona_id: str


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    tts_enabled: bool | None = None
    show_memory_hints: bool | None = None
    atom_memory_base_url: str | None = None
    default_warmth: int | None = None


@router.get("", response_model=SettingsOut)
async def get_settings(request: Request) -> SettingsOut:
    s = request.app.state.velora.settings
    return SettingsOut(
        llm_base_url=s.llm_base_url,
        llm_model=s.llm_model,
        llm_api_key_set=bool(s.llm_api_key),
        atom_memory_base_url=s.atom_memory_base_url,
        memory_space_uid=s.memory_space_uid,
        tts_enabled=s.tts_enabled,
        show_memory_hints=s.show_memory_hints,
        default_warmth=s.default_warmth,
        preset_id=s.preset_id,
        start_memory_sidecar=s.start_memory_sidecar,
        persona_name=DEFAULT_PERSONA.name,
        persona_id=DEFAULT_PERSONA.id,
    )


@router.put("", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate, request: Request) -> SettingsOut:
    state = request.app.state.velora
    s = state.settings
    if body.llm_base_url is not None:
        s.llm_base_url = body.llm_base_url.rstrip("/")
        state.provider.base_url = s.llm_base_url
    if body.llm_model is not None:
        s.llm_model = body.llm_model
    if body.llm_api_key is not None:
        s.llm_api_key = body.llm_api_key
        state.provider.api_key = s.llm_api_key
    if body.tts_enabled is not None:
        s.tts_enabled = body.tts_enabled
    if body.show_memory_hints is not None:
        s.show_memory_hints = body.show_memory_hints
    if body.atom_memory_base_url is not None:
        s.atom_memory_base_url = body.atom_memory_base_url.rstrip("/")
        state.memory.base_url = s.atom_memory_base_url
        state.memory._ready.clear()
    if body.default_warmth is not None:
        from server.core.persona.style import clamp_warmth

        s.default_warmth = clamp_warmth(body.default_warmth)

    await save_settings(state.store, s)
    return await get_settings(request)
