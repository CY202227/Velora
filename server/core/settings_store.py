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
    "atom_memory_base_url",
    "default_warmth",
)


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
            settings.tts_enabled = raw.strip().lower() in ("1", "true", "yes", "on")
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


async def save_settings(store: ConversationStore, settings: Settings) -> None:
    await store.set_setting("llm_base_url", settings.llm_base_url)
    await store.set_setting("llm_model", settings.llm_model)
    await store.set_setting("llm_api_key", settings.llm_api_key)
    await store.set_setting(
        "tts_enabled", "true" if settings.tts_enabled else "false"
    )
    await store.set_setting("atom_memory_base_url", settings.atom_memory_base_url)
    await store.set_setting("default_warmth", str(settings.default_warmth))
