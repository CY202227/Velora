"""Runtime settings for Velora server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
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

    @model_validator(mode="after")
    def protect_runtime_storage(self):
        workspace = Path(self.workspaces_dir).resolve()
        for name in ("execution_snapshots_dir", "execution_policy_dir", "execution_output_dir", "execution_receipts_dir",
                     "runtime_archive_dir", "control_token_path"):
            private = Path(getattr(self, name)).resolve()
            if private == workspace or private.is_relative_to(workspace):
                raise ValueError(f"{name} must stay outside writable workspaces")
        return self

    host: str = "127.0.0.1"
    port: int = 8030
    database_url: str = f"sqlite+aiosqlite:///{(_DATA / 'velora.db').as_posix()}"
    api_auth_enabled: bool = True
    control_token_path: str = f"{(_DATA / 'local-control.token').as_posix()}"

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
    memory_recall_method: Literal["bm25", "fuzzy", "llm", "embedding", "hybrid"] = "bm25"
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
    vertin_script: str = ""

    def resolve_vertin_script(self) -> str:
        if self.vertin_script:
            return self.vertin_script
        script = _DATA / "vertin" / "infer" / "infer.py"
        return str(script) if script.is_file() else ""

    vertin_python: str = ""
    # Show per-turn memory recall/write hints in Desk
    show_memory_hints: bool = True
    # 0 = more assistant, 100 = more companion (new sessions)
    default_warmth: int = 35
    default_persona_id: str = "daily-assistant"

    # local-default preset: spawn atom-memory on :8020 with Velora
    preset_id: str = "local-default"
    start_memory_sidecar: bool = True
    memory_required: bool = True
    atom_memory_project_dir: str = ""
    atom_memory_database_url: str = f"sqlite:///{(_DATA / 'atom_memory.db').as_posix()}"
    atom_memory_sidecar_log: str = f"{(_DATA / 'atom_memory_sidecar.log').as_posix()}"
    atom_memory_startup_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

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
    retired_round_limit: int | None = Field(default=None, validation_alias="VELORA_MAX_TOOL_ROUNDS", exclude=True, repr=False)
    retired_runtime_switch: bool | None = Field(default=None, validation_alias="VELORA_AGENT_RUNTIME_ENABLED", exclude=True, repr=False)
    retired_runtime_sessions: tuple[str, ...] | None = Field(default=None, validation_alias="VELORA_AGENT_RUNTIME_SESSIONS", exclude=True, repr=False)

    @model_validator(mode="after")
    def warn_retired_runtime_configuration(self):
        if any(value is not None for value in (self.retired_round_limit, self.retired_runtime_switch, self.retired_runtime_sessions)):
            import warnings
            warnings.warn("Retired runtime flags are ignored; all sessions use the unified runtime. Configure agent_max_steps only if an explicit step budget is required.", UserWarning, stacklevel=2)
        return self

    agent_max_steps: int | None = Field(default=None, gt=0)
    agent_context_window: int | None = None
    agent_output_reserve: int = 4096
    agent_run_timeout_seconds: float | None = Field(default=None, gt=0)
    agent_request_retries: int = 2
    agent_summary_output_tokens: int = 1024
    agent_token_count_path: str | None = None

    # MCP clients (JSON configuration with stdio, SSE, or streamable HTTP servers)
    mcp_enabled: bool = True
    mcp_config_path: str = f"{(_DATA / 'mcp_server.json').as_posix()}"

    # Computer tools require explicit enablement and an available execution boundary.
    computer_enabled: bool = False
    execution_location: Literal["local", "server"] = "local"
    execution_backend: Literal["auto", "native", "container"] = "auto"
    execution_require_approval: bool = False
    execution_allow_public_network: bool = False
    execution_allow_host: bool = False
    execution_read_allowlist: list[str] = Field(default_factory=list)
    execution_snapshots_enabled: bool = True
    execution_snapshots_dir: str = f"{(_DATA / 'file-checkpoints').as_posix()}"
    execution_snapshot_storage_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    execution_output_dir: str = f"{(_DATA / 'execution-output').as_posix()}"
    execution_output_storage_bytes: int = Field(default=128 * 1024 * 1024, gt=0)
    execution_output_capture_bytes: int = Field(default=1024 * 1024, ge=12000, le=4 * 1024 * 1024)
    execution_receipts_dir: str = f"{(_DATA / 'execution-receipts').as_posix()}"
    execution_receipts_storage_bytes: int = Field(default=128 * 1024 * 1024, gt=0)
    task_event_budget_bytes: int = Field(default=8 * 1024 * 1024, ge=4096)
    task_history_days: int = Field(default=30, ge=1)
    runtime_archive_dir: str = f"{(_DATA / 'runtime-archives').as_posix()}"
    runtime_archive_storage_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    runtime_database_max_bytes: int = Field(default=512 * 1024 * 1024, ge=32 * 1024 * 1024)
    runtime_checkpoint_max_bytes: int = Field(default=8 * 1024 * 1024, ge=4096)
    execution_container_image: str = "python:3.11-slim"
    execution_windows_runtime: str = f"{(_DATA / 'sandbox-runtime/windows/1.0.1').as_posix()}/{'arm64' if os.environ.get('PROCESSOR_ARCHITECTURE', '').lower() == 'arm64' else 'x64'}/wxc-exec.exe"
    execution_policy_dir: str = f"{(_DATA / 'execution-policies').as_posix()}"
    computer_shell: str = "powershell"  # powershell | cmd | bash
    computer_timeout_seconds: int = 60
    computer_allow_abs_paths: bool = False
    workspaces_dir: str = f"{(_DATA / 'workspaces').as_posix()}"

    # Optional read-only knowledge library (separate from personal memory).
    xwiki_enabled: bool = False
    xwiki_workspace: str = f"{(_DATA / 'wiki').as_posix()}"

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
