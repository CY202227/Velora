"""Settings and conversation rows survive via ConversationStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.config import Settings
from server.core.conversation.store import ConversationStore
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.settings_store import load_settings, save_settings


def test_computer_tools_are_disabled_by_default() -> None:
    assert Settings(_env_file=None).computer_enabled is False


@pytest.mark.asyncio
async def test_settings_roundtrip(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}"
    store = ConversationStore(url)
    await store.init()
    settings = Settings(
        llm_base_url="http://example.com/v1",
        llm_model="Qwen3.6",
        llm_api_key="EMPTY",
        tts_enabled=True,
    )
    provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key)
    await save_settings(store, settings)

    settings2 = Settings()  # defaults
    provider2 = OpenAICompatProvider(settings2.llm_base_url, settings2.llm_api_key)
    await load_settings(store, settings2, provider2)
    assert settings2.llm_base_url == "http://example.com/v1"
    assert settings2.llm_model == "Qwen3.6"
    assert settings2.llm_api_key == "EMPTY"
    assert settings2.tts_enabled is True
    assert provider2.base_url == "http://example.com/v1"
    await store.close()


@pytest.mark.asyncio
async def test_normalize_mysql_url() -> None:
    from server.core.conversation.store import normalize_database_url

    assert normalize_database_url("mysql://u:p@h/db").startswith("mysql+aiomysql://")
    assert "aiosqlite" in normalize_database_url("sqlite:///./x.db")
