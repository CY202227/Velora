"""Runtime settings for Velora server."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_ROOT = Path(__file__).resolve().parents[1]
_DATA = _ROOT / "velora_data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VELORA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8030
    database_url: str = f"sqlite+aiosqlite:///{(_DATA / 'velora.db').as_posix()}"

    # OpenAI-compatible LLM
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # atom-memory
    atom_memory_base_url: str = "http://127.0.0.1:8020"
    atom_memory_api_key: str = ""
    memory_space_uid: str = "velora-default"
    memory_budget_chars: int = 400
    memory_max_atoms: int = 5
    consolidate_every_n_turns: int = 3

    # conversation
    history_max_messages: int = 40
    tts_enabled: bool = False
    # 0 = more assistant, 100 = more companion (new sessions)
    default_warmth: int = 35

    # local-default preset / optional atom-memory sidecar
    preset_id: str = "local-default"
    start_memory_sidecar: bool = False

    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"


def get_settings() -> Settings:
    _DATA.mkdir(parents=True, exist_ok=True)
    return Settings()
