"""Shared application state wired at startup."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from server.config import Settings
from server.core.chain.executor import ChainExecutor
from server.core.chain.locks import SessionLockManager
from server.core.chain.router import TurnRouter
from server.core.chain.wait_registry import WaitRegistry
from server.core.computer import register_computer_tools
from server.core.conversation.store import ConversationStore
from server.core.memory.client import AtomMemoryClient
from server.core.memory.consolidate import ConsolidateJob
from server.core.memory.sidecar import MemorySidecar
from server.core.mcp import McpManager
from server.core.nodes import (
    ComposePromptNode,
    EmitReplyNode,
    LLMNode,
    LoadContextNode,
    PersistTurnNode,
    PostProcessNode,
    RecallMemoryNode,
)
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.reminders.job import ReminderJob
from server.core.skills import SkillManager
from server.core.tools import ToolRegistry, register_builtin_tools
from server.core.tools.web_search import register_web_search_tool


@dataclass
class AppState:
    settings: Settings
    store: ConversationStore
    memory: AtomMemoryClient
    consolidate_job: ConsolidateJob
    router: TurnRouter
    provider: OpenAICompatProvider
    locks: SessionLockManager
    reminder_job: ReminderJob
    tools: ToolRegistry
    mcp: McpManager
    skills: SkillManager
    memory_sidecar: MemorySidecar | None = None


_COMPUTER_TOOL_NAMES = (
    "run_shell",
    "run_python",
    "read_file",
    "write_file",
    "list_dir",
)


def _ensure_skills_seed(settings: Settings) -> None:
    dest = Path(settings.skills_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        return
    root = Path(__file__).resolve().parents[1]
    seed = root / "presets" / "skills" / "hello"
    if seed.is_dir():
        shutil.copytree(seed, dest / "hello")


def sync_optional_tools(tools: ToolRegistry, settings: Settings) -> None:
    """Re-register computer / web_search after settings toggle."""
    for name in _COMPUTER_TOOL_NAMES:
        tools.remove(name)
    tools.remove("web_search")
    if settings.computer_enabled:
        register_computer_tools(tools)
    if settings.web_search_enabled:
        register_web_search_tool(tools)


def build_app_state(settings: Settings) -> AppState:
    store = ConversationStore(settings.database_url)
    memory = AtomMemoryClient(
        settings.atom_memory_base_url,
        settings.atom_memory_api_key,
    )
    consolidate_job = ConsolidateJob(
        memory,
        layer_every_n=settings.memory_layer_every_n_consolidates,
    )
    provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key)
    wait_registry = WaitRegistry()
    locks = SessionLockManager()
    tools = ToolRegistry()
    register_builtin_tools(tools)
    sync_optional_tools(tools, settings)
    mcp = McpManager(settings)
    _ensure_skills_seed(settings)
    skills = SkillManager(settings.skills_dir)

    nodes = [
        LoadContextNode(store, settings, memory),
        RecallMemoryNode(
            memory,
            max_atoms=settings.memory_max_atoms,
            budget_chars=settings.memory_budget_chars,
            policy=settings.memory_recall_policy,
        ),
        ComposePromptNode(tools, skills),
        LLMNode(
            provider,
            tools=tools,
            max_tool_rounds=settings.max_tool_rounds,
        ),
        PostProcessNode(),
        PersistTurnNode(
            store,
            memory,
            consolidate_job,
            consolidate_every_n=settings.consolidate_every_n_turns,
        ),
        EmitReplyNode(),
    ]
    executor = ChainExecutor(nodes, wait_registry=wait_registry)
    router = TurnRouter(executor, locks, wait_registry)
    reminder_job = ReminderJob(store, settings, router)
    return AppState(
        settings=settings,
        store=store,
        memory=memory,
        consolidate_job=consolidate_job,
        router=router,
        provider=provider,
        locks=locks,
        reminder_job=reminder_job,
        tools=tools,
        mcp=mcp,
        skills=skills,
    )
