"""Shared application state wired at startup."""

from __future__ import annotations

from dataclasses import dataclass

from server.config import Settings
from server.core.chain.executor import ChainExecutor
from server.core.chain.locks import SessionLockManager
from server.core.chain.router import TurnRouter
from server.core.chain.wait_registry import WaitRegistry
from server.core.conversation.store import ConversationStore
from server.core.memory.client import AtomMemoryClient
from server.core.memory.consolidate import ConsolidateJob
from server.core.memory.sidecar import MemorySidecar
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


@dataclass
class AppState:
    settings: Settings
    store: ConversationStore
    memory: AtomMemoryClient
    consolidate_job: ConsolidateJob
    router: TurnRouter
    provider: OpenAICompatProvider
    memory_sidecar: MemorySidecar | None = None


def build_app_state(settings: Settings) -> AppState:
    store = ConversationStore(settings.database_url)
    memory = AtomMemoryClient(
        settings.atom_memory_base_url,
        settings.atom_memory_api_key,
    )
    consolidate_job = ConsolidateJob(memory)
    provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key)
    wait_registry = WaitRegistry()
    locks = SessionLockManager()

    nodes = [
        LoadContextNode(store, settings),
        RecallMemoryNode(
            memory,
            max_atoms=settings.memory_max_atoms,
            budget_chars=settings.memory_budget_chars,
        ),
        ComposePromptNode(),
        LLMNode(provider),
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
    return AppState(
        settings=settings,
        store=store,
        memory=memory,
        consolidate_job=consolidate_job,
        router=router,
        provider=provider,
    )
