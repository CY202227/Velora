from unittest.mock import AsyncMock
import asyncio

import pytest

from server.core.agent.store import RunStore
from server.core.agent.state import RunState, RunPhase, RunStatus
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.conversation.store import ConversationStore
from server.core.agent.tool_round import execute_tool_round
from server.core.provider.base import ChatResult, ToolCall
from server.core.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_checkpoints_survive_reopen_and_unknown_tools_block_replay(tmp_path):
    url = "sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix()
    db = ConversationStore(url)
    await db.init()
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", llm_api_key="secret-key")
    ctx.agent_run = RunState(messages=[{"role": "user", "content": "task secret-key"}])
    ctx.agent_run.phase = RunPhase.REQUESTING
    store = RunStore(db._session_factory)
    try:
        await store.checkpoint(ctx, "request_ready")
        ctx.agent_run.tool_executions.append({"id": "a", "name": "write", "round": 0, "status": "running"})
        ctx.agent_run.phase = RunPhase.EXECUTING_TOOLS
        await store.checkpoint(ctx, "tool_started")
        run_id = ctx.agent_run.run_id
    finally:
        await db.close()
    reopened = ConversationStore(url)
    await reopened.init()
    try:
        data = await RunStore(reopened._session_factory).read(run_id)
        assert data["revision"] == 2
        assert data["run"]["tool_executions"][0]["status"] == "result_unknown"
        assert not data["replay_safe"]
        assert "secret-key" not in str(data)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_terminal_checkpoint_is_not_replayable(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "terminal.db").as_posix())
    await db.init()
    try:
        ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s")
        ctx.agent_run = RunState(phase=RunPhase.REQUESTING)
        store = RunStore(db._session_factory)
        await store.checkpoint(ctx, "request_ready")
        assert (await store.read(ctx.agent_run.run_id))["replay_safe"]
        ctx.agent_run.finish(RunStatus.COMPLETED)
        await store.checkpoint(ctx, "model_stage_end")
        assert not (await store.read(ctx.agent_run.run_id))["replay_safe"]
        assert len(await store.recent()) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_checkpoint_prevents_tool_dispatch(monkeypatch):
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", agent_run=RunState())
    ctx.checkpoint = AsyncMock(side_effect=OSError("database unavailable"))
    execute = AsyncMock()
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", execute)
    with pytest.raises(OSError):
        await execute_tool_round(ctx, ToolRegistry(), [], ChatResult(tool_calls=[ToolCall("a", "write", "{}")]), 0)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_claim_is_single_use_and_creates_successor(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "claim.db").as_posix())
    await db.init()
    try:
        ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", model="m", agent_run=RunState(phase=RunPhase.REQUESTING))
        store = RunStore(db._session_factory)
        await store.checkpoint(ctx, "request_ready")
        results = await asyncio.gather(store.claim(ctx.agent_run.run_id), store.claim(ctx.agent_run.run_id), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        child = next(r for r in results if isinstance(r, dict))
        assert child["parent_run_id"] == ctx.agent_run.run_id
        assert (await store.read(child["run"]["run_id"]))["replay_safe"]
        with pytest.raises(ValueError):
            await store.checkpoint(ctx, "late_original_write")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_waiting_recovery_requires_input_and_keeps_original_goal(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "wait.db").as_posix())
    await db.init()
    try:
        ctx = TurnContext(request=TurnRequest("s", "original goal"), session_id="s", agent_run=RunState(messages=[{"role": "user", "content": "original goal"}]))
        ctx.agent_run.finish(RunStatus.WAITING, "need input")
        store = RunStore(db._session_factory)
        await store.checkpoint(ctx, "model_stage_end")
        with pytest.raises(ValueError):
            await store.claim(ctx.agent_run.run_id)
        child = await store.claim(ctx.agent_run.run_id, "additional input")
        assert child["user_text"] == "original goal"
        assert child["run"]["messages"][-1]["content"] == "additional input"
        assert child["run"]["outcome"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_safe_checkpoint_actually_resumes_model_through_router(tmp_path):
    from server.core.chain.router import TurnRouter
    from server.core.chain.executor import ChainExecutor
    from server.core.chain.locks import SessionLockManager
    from server.core.chain.wait_registry import WaitRegistry
    from server.core.chain.types import NodeResult
    from server.core.nodes.llm import LLMNode
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "resume.db").as_posix())
    await db.init()
    try:
        source = TurnContext(request=TurnRequest("s", "original task"), session_id="s", model="m", agent_run=RunState(phase=RunPhase.REQUESTING, step=1, messages=[{"role": "user", "content": "original task"}]))
        store = RunStore(db._session_factory)
        await store.checkpoint(source, "request_ready")
        class Prepare:
            name = "Prepare"
            async def process(self, ctx):
                ctx.model = "m"
                ctx.llm_base_url = "http://localhost"
                ctx.messages = [{"role": "user", "content": "rebuilt history"}]
                return NodeResult.CONTINUE
        node = LLMNode(object(), context_window=32000)
        async def sample(ctx, provider, messages, **kwargs):
            assert messages[0]["content"] == "original task"
            return ChatResult(content="recovered answer")
        node._stream_round = sample
        router = TurnRouter(ChainExecutor([Prepare(), node]), SessionLockManager(), WaitRegistry(), run_store=store)
        result = await router.handle(TurnRequest("s", ""), resume_run_id=source.agent_run.run_id)
        assert result.assistant_text == "recovered answer"
        assert result.agent_run.run_id != source.agent_run.run_id
        assert result.agent_run.step == 1
        with pytest.raises(ValueError):
            await router.handle(TurnRequest("s", ""), resume_run_id=source.agent_run.run_id)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_exchange_operation_does_not_duplicate_saved_turns(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "exchange.db").as_posix())
    await db.init()
    try:
        session = await db.create_session(persona_id="p", memory_space_uid="m", model="m")
        first, _ = await db.save_exchange(session.id, "question", "answer", operation_id="run-one")
        second, _ = await db.save_exchange(session.id, "question", "answer", operation_id="run-one")
        assert first.id == second.id
        assert await db.count_turns(session.id) == 2
        with pytest.raises(ValueError):
            await db.save_exchange(session.id, "question", "different answer", operation_id="run-one")
    finally:
        await db.close()
