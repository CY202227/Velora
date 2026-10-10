"""Runtime settings for Velora server."""

from __future__ import annotations

import os
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
    # layered = L3 sticky + L2 + L1 (atom-memory default); flat = old retrieve
    memory_recall_policy: str = "layered"
    consolidate_every_n_turns: int = 3
    # After N scheduled L1 consolidates, also run L2 synthesize + L3 persona
    memory_layer_every_n_consolidates: int = 5

    # conversation
    history_max_messages: int = 40
    # Keep the newest turns verbatim; compact older in-window turns into a
    # bounded, non-durable transcript excerpt for the current request.
    history_recent_messages: int = 16
    history_compaction_chars: int = 1600
    tts_enabled: bool = False
    # Show per-turn memory recall/write hints in Desk
    show_memory_hints: bool = True
    # 0 = more assistant, 100 = more companion (new sessions)
    default_warmth: int = 35
    default_persona_id: str = "daily-assistant"

    # local-default preset: spawn atom-memory on :8020 with Velora
    preset_id: str = "local-default"
    start_memory_sidecar: bool = True

    # Builtin local LLM (GGUF + llama-server sidecar)
    local_llm_enabled: bool = False
    local_llm_base_url: str = "http://127.0.0.1:8040/v1"
    local_llm_model: str = "qwen3.8-4b-distill"
    local_llm_port: int = 8040
    local_llm_ctx: int = 4096
    start_local_llm_sidecar: bool = False
    models_dir: str = f"{(_DATA / 'models').as_posix()}"
    bin_dir: str = f"{(_DATA / 'bin').as_posix()}"

    # reminders (explicit; quiet hours in user timezone)
    reminders_enabled: bool = True
    reminder_poll_seconds: int = 20
    user_timezone: str = "Asia/Shanghai"
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"

    # function calling / builtin tools
    max_tool_rounds: int = 4

    # MCP clients (JSON configuration with stdio, SSE, or streamable HTTP servers)
    mcp_enabled: bool = True
    mcp_config_path: str = f"{(_DATA / 'mcp_server.json').as_posix()}"

    # Local computer use is powerful and not sandboxed yet. It must be enabled
    # explicitly by the person running Velora.
    computer_enabled: bool = False
    computer_shell: str = "powershell"  # powershell | cmd | bash
    computer_timeout_seconds: int = 60
    computer_allow_abs_paths: bool = False
    workspaces_dir: str = f"{(_DATA / 'workspaces').as_posix()}"

    # skills
    skills_dir: str = f"{(_DATA / 'skills').as_posix()}"

    # web search (Tavily)
    web_search_enabled: bool = True
    tavily_api_key: str = ""

    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"


def get_settings() -> Settings:
    _DATA.mkdir(parents=True, exist_ok=True)
    # Lets a clean demo use a dedicated config without replacing a developer's
    # real .env (for example: VELORA_ENV_FILE=.env.demo).
    settings = Settings(_env_file=os.environ.get("VELORA_ENV_FILE", ".env"))
    Path(settings.models_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.bin_dir).mkdir(parents=True, exist_ok=True)
    return settings
