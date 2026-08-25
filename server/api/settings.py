from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server.app_state import sync_optional_tools
from server.core.persona.default import DEFAULT_PERSONA
from server.core.reminders.quiet import parse_hhmm, resolve_timezone
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
    user_timezone: str
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str
    reminders_enabled: bool
    preset_id: str
    start_memory_sidecar: bool
    persona_name: str
    persona_id: str
    persona_opener: str | None = None
    web_search_enabled: bool = True
    tavily_api_key_set: bool = False
    computer_enabled: bool = True
    skills_dir: str = ""
    mcp_enabled: bool = True
    local_llm_enabled: bool = False
    local_llm_base_url: str = ""
    local_llm_model: str = ""
    start_local_llm_sidecar: bool = False


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    tts_enabled: bool | None = None
    show_memory_hints: bool | None = None
    atom_memory_base_url: str | None = None
    default_warmth: int | None = None
    user_timezone: str | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    web_search_enabled: bool | None = None
    tavily_api_key: str | None = None
    computer_enabled: bool | None = None


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
        user_timezone=s.user_timezone,
        quiet_hours_enabled=s.quiet_hours_enabled,
        quiet_hours_start=s.quiet_hours_start,
        quiet_hours_end=s.quiet_hours_end,
        reminders_enabled=s.reminders_enabled,
        preset_id=s.preset_id,
        start_memory_sidecar=s.start_memory_sidecar,
        persona_name=DEFAULT_PERSONA.name,
        persona_id=DEFAULT_PERSONA.id,
        persona_opener=DEFAULT_PERSONA.opener,
        web_search_enabled=s.web_search_enabled,
        tavily_api_key_set=bool(s.tavily_api_key),
        computer_enabled=s.computer_enabled,
        skills_dir=s.skills_dir,
        mcp_enabled=s.mcp_enabled,
        local_llm_enabled=s.local_llm_enabled,
        local_llm_base_url=s.local_llm_base_url,
        local_llm_model=s.local_llm_model,
        start_local_llm_sidecar=s.start_local_llm_sidecar,
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
    if body.user_timezone is not None:
        tz = body.user_timezone.strip()
        resolve_timezone(tz)
        s.user_timezone = tz or s.user_timezone
    if body.quiet_hours_enabled is not None:
        s.quiet_hours_enabled = body.quiet_hours_enabled
    if body.quiet_hours_start is not None:
        try:
            parse_hhmm(body.quiet_hours_start)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        s.quiet_hours_start = body.quiet_hours_start.strip()
    if body.quiet_hours_end is not None:
        try:
            parse_hhmm(body.quiet_hours_end)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        s.quiet_hours_end = body.quiet_hours_end.strip()
    if body.web_search_enabled is not None:
        s.web_search_enabled = body.web_search_enabled
    if body.tavily_api_key is not None:
        s.tavily_api_key = body.tavily_api_key
    if body.computer_enabled is not None:
        s.computer_enabled = body.computer_enabled

    await save_settings(state.store, s)
    sync_optional_tools(state.tools, s)
    return await get_settings(request)
