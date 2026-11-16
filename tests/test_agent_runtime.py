"""Runtime boundaries without network, database, or model dependencies."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.core.agent.state import RunPhase, RunState, RunStatus
from server.core.chain.context import TurnContext
from server.core.chain.executor import ChainExecutor
from server.core.chain.types import NodeResult, TurnRequest
from server.core.nodes.llm import LLMNode
from server.core.provider.base import ChatResult, ToolCall
from server.core.provider.base import TokenUsage
from server.core.agent.context_budget import prune_tool_results
from server.core.tools.registry import ToolRegistry


def context():
    return TurnContext(
        request=TurnRequest("s", "hello"), session_id="s",
        llm_base_url="http://localhost", messages=[{"role": "user", "content": "hello"}],
    )


@pytest.mark.asyncio
async def test_tool_round_history_and_event_order():
    ctx = context()
    events = []
    async def emit(event):
        events.append(event.type)
    ctx.emit = emit
    ctx.extras["tools"] = [{"type": "function"}]
    provider = object()
    node = LLMNode(provider, tools=ToolRegistry(), max_steps=2, context_window=32000)
    calls = []
    async def sample(ctx, provider, messages, *, tools):
        calls.append(tools)
        assert ctx.agent_run.phase is RunPhase.REQUESTING
        if len(calls) == 1:
            return ChatResult(tool_calls=[ToolCall("c", "missing", "{}")])
        assert messages[-1]["tool_call_id"] == "c"
        assert 'unknown tool' in messages[-1]["content"]
        return ChatResult(content="done")
    node._stream_round = sample
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert calls == [[{"type": "function"}], [{"type": "function"}]]
    assert events == ["assistant_draft_reset", "tool_call", "tool_result"]
    assert ctx.agent_run.messages is ctx.messages
    assert ctx.agent_run.step == 2
    assert ctx.agent_run.outcome.status is RunStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,expected", [(ChatResult(content="done"), 1), (ChatResult(), 0)])
async def test_chain_runs_downstream_once_only_on_success(reply, expected):
    ctx = context()
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock(return_value=reply)
    downstream = []
    for name in ("PostProcess", "PersistTurn", "EmitReply"):
        downstream.append(type("Node", (), {"name": name, "process": AsyncMock(return_value=NodeResult.CONTINUE)})())
    await ChainExecutor([node, *downstream]).execute(ctx)
    assert all(n.process.await_count == expected for n in downstream)
    assert ctx.agent_run.outcome.status is (RunStatus.COMPLETED if expected else RunStatus.FAILED)


@pytest.mark.asyncio
async def test_model_error_and_exhaustion_have_distinct_outcomes():
    ctx = context()
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock(side_effect=ValueError("bad request"))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.reason == "llm_error"
    node = LLMNode(object(), tools=ToolRegistry(), max_steps=1, context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(tool_calls=[ToolCall("c", "missing", "{}")]))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.reason == "step_budget_exhausted"
    assert ctx.stop_reason == "step_budget_exhausted"


@pytest.mark.asyncio
async def test_cancellation_propagates_without_downstream_work():
    ctx = context()
    node = LLMNode(object(), context_window=32000)
    entered = asyncio.Event()
    async def sample(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    node._stream_round = sample
    task = asyncio.create_task(node.process(ctx))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.agent_run.outcome.status is RunStatus.CANCELLED


def test_terminal_state_cannot_be_advanced_or_finished_again():
    state = RunState()
    state.finish(RunStatus.COMPLETED)
    with pytest.raises(RuntimeError):
        state.enter(RunPhase.REQUESTING)
    with pytest.raises(RuntimeError):
        state.finish(RunStatus.FAILED)


@pytest.mark.asyncio
async def test_missing_configuration_stops_without_a_model_request():
    ctx = context()
    ctx.llm_base_url = ""
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock()
    assert await node.process(ctx) is NodeResult.STOP
    node._stream_round.assert_not_awaited()
    assert ctx.agent_run.step == 0
    assert ctx.agent_run.outcome.reason == "llm_not_configured"


@pytest.mark.asyncio
async def test_long_task_and_usage_are_separate_from_context_occupancy():
    ctx = context()
    node = LLMNode(object(), tools=ToolRegistry(), max_steps=8, context_window=32000)
    replies = [ChatResult(tool_calls=[ToolCall(str(i), "missing", '{"step":' + str(i) + '}')], usage=TokenUsage(100, 10, 50)) for i in range(5)]
    replies.append(ChatResult(content="done", usage=TokenUsage(120, 20, 60)))
    node._stream_round = AsyncMock(side_effect=replies)
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.agent_run.step == 6
    assert ctx.agent_run.input_tokens == 620
    assert ctx.agent_run.latest_input_tokens == 120
    assert ctx.agent_run.cached_input_tokens == 310


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity,reason", [(None, "context_capacity_unknown"), (100, "context_budget_exhausted")])
async def test_context_admission_prevents_unbounded_requests(capacity, reason):
    ctx = context()
    node = LLMNode(object(), max_steps=8, context_window=capacity)
    node._stream_round = AsyncMock()
    assert await node.process(ctx) is NodeResult.STOP
    node._stream_round.assert_not_awaited()
    assert ctx.agent_run.outcome.reason == reason


@pytest.mark.asyncio
async def test_time_budget_is_not_recorded_as_user_cancellation():
    ctx = context()
    node = LLMNode(object(), max_steps=8, context_window=32000, run_timeout=0.01)
    async def sample(*args, **kwargs):
        await asyncio.Event().wait()
    node._stream_round = sample
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.status is RunStatus.FAILED
    assert ctx.agent_run.outcome.reason == "runtime_timeout"


@pytest.mark.asyncio
async def test_transient_request_retries_without_replaying_tools(monkeypatch):
    import httpx
    ctx = context()
    node = LLMNode(object(), max_steps=8, context_window=32000)
    node._stream_round = AsyncMock(side_effect=[httpx.ConnectError("offline"), ChatResult(content="done")])
    monkeypatch.setattr("server.core.agent.loop.asyncio.sleep", AsyncMock())
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.agent_run.retries == 1
    assert ctx.agent_run.step == 1


def test_pruning_keeps_recent_evidence_and_tool_pairing():
    messages = [{"role": "assistant", "tool_calls": [{"id": "a"}]}, {"role": "tool", "tool_call_id": "a", "content": "x" * 5000}, {"role": "tool", "tool_call_id": "b", "content": "y" * 5000}]
    assert prune_tool_results(messages) == 1
    assert messages[1]["tool_call_id"] == "a"
    assert messages[2]["content"] == "y" * 5000


def test_usage_parsing_does_not_double_count_cached_input():
    usage = TokenUsage.from_payload({"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 80}})
    assert usage == TokenUsage(100, 20, 80)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["local-model", "remote-model"])
async def test_unbounded_runtime_keeps_tools_after_twenty_four_steps(model):
    ctx = context()
    ctx.model = model
    ctx.extras["tools"] = [{"type": "function"}]
    node = LLMNode(object(), tools=ToolRegistry(), context_window=100000)
    calls = 0
    async def sample(ctx, provider, messages, *, tools):
        nonlocal calls
        calls += 1
        assert tools is not None
        if calls <= 30:
            return ChatResult(tool_calls=[ToolCall(str(calls), "missing", '{"step":' + str(calls) + '}')])
        return ChatResult(content="done")
    node._stream_round = sample
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.agent_run.step == 31
    assert node._run_timeout is None


@pytest.mark.asyncio
async def test_explicit_step_budget_stops_before_next_request():
    ctx = context()
    node = LLMNode(object(), tools=ToolRegistry(), max_steps=2, context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(tool_calls=[ToolCall("c", "missing", "{}")]))
    assert await node.process(ctx) is NodeResult.STOP
    assert node._stream_round.await_count == 2
    assert ctx.agent_run.outcome.reason == "step_budget_exhausted"


@pytest.mark.asyncio
async def test_unbounded_runtime_can_still_be_cancelled():
    ctx = context()
    node = LLMNode(object(), context_window=32000)
    entered = asyncio.Event()
    async def sample(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    node._stream_round = sample
    task = asyncio.create_task(node.process(ctx))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.agent_run.outcome.status is RunStatus.CANCELLED


def test_optional_budget_defaults_and_invalid_config():
    from pydantic import ValidationError
    from server.config import Settings
    settings = Settings(_env_file=None, agent_max_steps=None, agent_run_timeout_seconds=None)
    assert settings.agent_max_steps is None
    assert settings.agent_run_timeout_seconds is None
    for config in ({"agent_max_steps": 0}, {"agent_run_timeout_seconds": -1}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **config)


def test_retired_flags_cannot_reactivate_four_round_mode():
    from server.config import Settings
    with pytest.warns(UserWarning, match="Retired runtime flags"):
        settings = Settings(_env_file=None, VELORA_MAX_TOOL_ROUNDS=4, VELORA_AGENT_RUNTIME_ENABLED=False, VELORA_AGENT_RUNTIME_SESSIONS=[])
    assert settings.agent_max_steps is None
    assert "retired_round_limit" not in settings.model_dump()
    assert not hasattr(settings, "agent_runtime_enabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_partial_output_never_reaches_success_or_executes_tools(reason):
    ctx = context()
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(content="partial answer", tool_calls=[ToolCall("c", "missing", "{}")], finish_reason=reason))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.status is RunStatus.FAILED
    assert ctx.assistant_text == ""
    assert not ctx.agent_run.tool_executions


@pytest.mark.asyncio
async def test_tool_cancel_distinguishes_started_and_unstarted_calls(monkeypatch):
    ctx = context()
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(tool_calls=[ToolCall("a", "first", "{}"), ToolCall("b", "second", "{}")]))
    entered = asyncio.Event()
    async def execute(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", execute)
    task = asyncio.create_task(node.process(ctx))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r["status"] for r in ctx.agent_run.tool_executions] == ["result_unknown", "not_started"]
