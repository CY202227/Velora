"""Persist user-facing settings in SQLite (survive process restart)."""

from __future__ import annotations

from server.config import Settings
from server.core.conversation.store import ConversationStore
from server.core.provider.openai_compat import OpenAICompatProvider

_KEYS = (
    "llm_base_url",
    "llm_model",
    "llm_api_key",
    "tts_enabled",
    "show_memory_hints",
    "atom_memory_base_url",
    "default_warmth",
    "user_timezone",
    "quiet_hours_enabled",
    "quiet_hours_start",
    "quiet_hours_end",
    "web_search_enabled",
    "tavily_api_key",
    "computer_enabled",
    "local_llm_enabled",
)


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def load_settings(
    store: ConversationStore,
    settings: Settings,
    provider: OpenAICompatProvider,
) -> None:
    """Overlay DB values onto in-memory settings + provider."""
    for key in _KEYS:
        raw = await store.get_setting(key)
        if raw is None:
            continue
        if key == "tts_enabled":
            settings.tts_enabled = _as_bool(raw)
        elif key == "show_memory_hints":
            settings.show_memory_hints = _as_bool(raw)
        elif key == "quiet_hours_enabled":
            settings.quiet_hours_enabled = _as_bool(raw)
        elif key == "web_search_enabled":
            settings.web_search_enabled = _as_bool(raw)
        elif key == "computer_enabled":
            settings.computer_enabled = _as_bool(raw)
        elif key == "local_llm_enabled":
            settings.local_llm_enabled = _as_bool(raw)
            provider.use_local_sampling = settings.local_llm_enabled
        elif key == "llm_base_url":
            settings.llm_base_url = raw.rstrip("/")
            provider.base_url = settings.llm_base_url
        elif key == "llm_model":
            settings.llm_model = raw
        elif key == "llm_api_key":
            settings.llm_api_key = raw
            provider.api_key = settings.llm_api_key
        elif key == "atom_memory_base_url":
            settings.atom_memory_base_url = raw.rstrip("/")
        elif key == "default_warmth":
            from server.core.persona.style import clamp_warmth

            settings.default_warmth = clamp_warmth(raw)
        elif key == "user_timezone":
            settings.user_timezone = raw.strip() or settings.user_timezone
        elif key == "quiet_hours_start":
            settings.quiet_hours_start = raw.strip() or settings.quiet_hours_start
        elif key == "quiet_hours_end":
            settings.quiet_hours_end = raw.strip() or settings.quiet_hours_end
        elif key == "tavily_api_key":
            settings.tavily_api_key = raw

    provider.use_local_sampling = settings.local_llm_enabled


async def save_settings(store: ConversationStore, settings: Settings) -> None:
    await store.set_setting("llm_base_url", settings.llm_base_url)
    await store.set_setting("llm_model", settings.llm_model)
    await store.set_setting("llm_api_key", settings.llm_api_key)
    await store.set_setting(
        "tts_enabled", "true" if settings.tts_enabled else "false"
    )
    await store.set_setting(
        "show_memory_hints",
        "true" if settings.show_memory_hints else "false",
    )
    await store.set_setting("atom_memory_base_url", settings.atom_memory_base_url)
    await store.set_setting("default_warmth", str(settings.default_warmth))
    await store.set_setting("user_timezone", settings.user_timezone)
    await store.set_setting(
        "quiet_hours_enabled",
        "true" if settings.quiet_hours_enabled else "false",
    )
    await store.set_setting("quiet_hours_start", settings.quiet_hours_start)
    await store.set_setting("quiet_hours_end", settings.quiet_hours_end)
    await store.set_setting(
        "web_search_enabled",
        "true" if settings.web_search_enabled else "false",
    )
    await store.set_setting("tavily_api_key", settings.tavily_api_key)
    await store.set_setting(
        "computer_enabled",
        "true" if settings.computer_enabled else "false",
    )
    await store.set_setting(
        "local_llm_enabled",
        "true" if settings.local_llm_enabled else "false",
    )
