"""Chain unit tests with fake LLM and fake memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.core.chain.context import TurnContext
from server.core.chain.executor import ChainExecutor
from server.core.chain.locks import SessionLockManager
from server.core.chain.router import TurnRouter
from server.core.chain.types import NodeResult, TurnRequest
from server.core.chain.wait_registry import WaitRegistry
from server.core.conversation.store import ConversationStore
from server.core.nodes.compose_prompt import ComposePromptNode
from server.core.nodes.emit_reply import EmitReplyNode
from server.core.nodes.load_context import LoadContextNode
from server.core.nodes.persist_turn import PersistTurnNode
from server.core.nodes.post_process import PostProcessNode
from server.core.nodes.recall_memory import RecallMemoryNode
from server.core.persona.default import DEFAULT_PERSONA
from server.config import Settings


class FakeMemory:
    def __init__(self) -> None:
        self.sources: list[dict] = []
        self.consolidated = 0

    async def recall(self, uid: str, query: str, **kwargs):
        del uid, kwargs
        if "老张" in query:
            return {
                "context_block": '<recalled_memory>\n<item key="n" kind="person">称呼老张</item>\n</recalled_memory>',
                "chars_used": 10,
            }
        return {"context_block": "", "hits": []}

    async def add_source(self, uid: str, **kwargs):
        del uid
        self.sources.append(kwargs)
        return {"id": "src-1"}

    async def consolidate(self, uid: str, **kwargs):
        del uid, kwargs
        self.consolidated += 1
        return {"status": "succeeded", "atoms_touched": 1}


class FakeConsolidateJob:
    def __init__(self, memory: FakeMemory) -> None:
        self.memory = memory

    async def enqueue(self, space_uid: str, trigger: str = "scheduled") -> None:
        await self.memory.consolidate(space_uid, trigger=trigger)

    async def run_now(self, space_uid: str, trigger: str = "correction") -> None:
        await self.memory.consolidate(space_uid, trigger=trigger)


class FakeLLMNode:
    name = "LLM"

    async def process(self, ctx: TurnContext) -> NodeResult:
        mem_hint = "（已见表述称呼）" if "老张" in ctx.memory_block else ""
        ctx.assistant_text = f"好的，收到。{mem_hint}"
        await ctx.publish("token", {"text": ctx.assistant_text})
        return NodeResult.CONTINUE


@pytest.fixture
async def store(tmp_path: Path) -> ConversationStore:
    db = (tmp_path / "t.db").as_posix()
    s = ConversationStore(f"sqlite+aiosqlite:///{db}")
    await s.init()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_chain_happy_path(store: ConversationStore) -> None:
    settings = Settings(
        llm_api_key="test",
        llm_model="fake",
        memory_space_uid="velora-test",
        consolidate_every_n_turns=1,
    )
    memory = FakeMemory()
    job = FakeConsolidateJob(memory)
    session = await store.create_session(
        persona_id=DEFAULT_PERSONA.id,
        memory_space_uid="velora-test",
        model="fake",
    )

    # Monkeypatch RecallMemoryNode / PersistTurnNode to use FakeMemory duck typing
    recall = RecallMemoryNode(memory)  # type: ignore[arg-type]
    persist = PersistTurnNode(store, memory, job, consolidate_every_n=1)  # type: ignore[arg-type]

    nodes = [
        LoadContextNode(store, settings),
        recall,
        ComposePromptNode(),
        FakeLLMNode(),
        PostProcessNode(),
        persist,
        EmitReplyNode(),
    ]
    router = TurnRouter(ChainExecutor(nodes), SessionLockManager(), WaitRegistry())
    events: list[str] = []

    async def emit(ev):
        events.append(ev.type)

    ctx = await router.handle(
        TurnRequest(session_id=session.id, user_text="你好，叫我老张"),
        emit=emit,
    )
    assert ctx.assistant_text.startswith("好的")
    assert ctx.memory_wrote
    assert len(memory.sources) == 1
    assert memory.sources[0]["kind"] == "turn"
    assert "token" in events
    assert "message" in events
    assert "turn_end" in events

    turns = await store.list_turns(session.id)
    assert len(turns) == 2
    assert turns[0].role == "user"


@pytest.mark.asyncio
async def test_compose_includes_memory_and_persona() -> None:
    ctx = TurnContext(
        request=TurnRequest("s", "hi"),
        session_id="s",
        persona=DEFAULT_PERSONA,
        memory_block="<recalled_memory>x</recalled_memory>",
        history=[{"role": "user", "content": "之前"}, {"role": "assistant", "content": "嗯"}],
    )
    result = await ComposePromptNode().process(ctx)
    assert result is NodeResult.CONTINUE
    assert ctx.messages[0]["role"] == "system"
    assert "日常助理" in DEFAULT_PERSONA.system_prompt or "贴心" in ctx.messages[0]["content"]
    assert "<recalled_memory>" in ctx.messages[0]["content"]
    assert "熟人助理" in ctx.messages[0]["content"] or "风格" in ctx.messages[0]["content"]
    assert ctx.messages[-1] == {"role": "user", "content": "hi"}
    # system + begin_dialogs (2) + history (2) + current user
    assert len(ctx.messages) == 6
    assert ctx.messages[1]["role"] == "user"
    assert ctx.messages[2]["role"] == "assistant"


@pytest.mark.asyncio
async def test_session_lock_serializes(store: ConversationStore) -> None:
    settings = Settings(llm_api_key="x", memory_space_uid="t")
    memory = FakeMemory()
    job = FakeConsolidateJob(memory)
    session = await store.create_session(
        persona_id=DEFAULT_PERSONA.id,
        memory_space_uid="t",
        model="fake",
    )
    nodes = [
        LoadContextNode(store, settings),
        RecallMemoryNode(memory),  # type: ignore[arg-type]
        ComposePromptNode(),
        FakeLLMNode(),
        PostProcessNode(),
        PersistTurnNode(store, memory, job, consolidate_every_n=99),  # type: ignore[arg-type]
        EmitReplyNode(),
    ]
    router = TurnRouter(ChainExecutor(nodes), SessionLockManager(), WaitRegistry())

    import asyncio

    async def one(text: str):
        return await router.handle(TurnRequest(session.id, text))

    await asyncio.gather(one("a"), one("b"))
    turns = await store.list_turns(session.id)
    assert len(turns) == 4
