from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core.agent.policy import StopAction, StopDecision
from server.core.agent.state import RunStatus
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest, NodeResult
from server.core.nodes.llm import LLMNode
from server.core.provider.base import ChatResult, ToolCall
from server.core.tools.registry import ToolRegistry


def make_node(policy=None):
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", llm_base_url="http://localhost", messages=[{"role": "user", "content": "task"}])
    node = LLMNode(object(), context_window=32000, stop_policies=(policy,) if policy else (), tools=ToolRegistry())
    return ctx, node


@pytest.mark.asyncio
async def test_completion_feedback_continues_then_finishes():
    policy = SimpleNamespace(on_stop=AsyncMock(side_effect=[StopDecision(StopAction.CONTINUE, "Missing verification"), StopDecision()]))
    ctx, node = make_node(policy)
    node._stream_round = AsyncMock(side_effect=[ChatResult(content="draft"), ChatResult(content="verified")])
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.assistant_text == "verified"
    assert ctx.agent_run.forced_continuations == 1
    assert ctx.messages[-1]["content"] == "Missing verification"


@pytest.mark.asyncio
@pytest.mark.parametrize("feedback", ["", "still missing"])
async def test_forced_continuation_is_bounded(feedback):
    policy = SimpleNamespace(on_stop=AsyncMock(return_value=StopDecision(StopAction.CONTINUE, feedback)))
    ctx, node = make_node(policy)
    node._stream_round = AsyncMock(return_value=ChatResult(content="answer"))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.reason == "continuation_limit"
    assert node._stream_round.await_count <= 4


@pytest.mark.asyncio
async def test_wait_does_not_persist_an_unfinished_answer():
    policy = SimpleNamespace(on_stop=AsyncMock(return_value=StopDecision(StopAction.WAIT, "Need recipient")))
    ctx, node = make_node(policy)
    node._stream_round = AsyncMock(return_value=ChatResult(content="draft"))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.status is RunStatus.WAITING
    assert ctx.assistant_text == ""


@pytest.mark.asyncio
async def test_identical_tool_failures_stop_but_do_not_replay_more_calls():
    ctx, node = make_node()
    node._stream_round = AsyncMock(return_value=ChatResult(tool_calls=[ToolCall("c", "missing", "{}")]))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.reason == "repeated_tool_failure"
    assert node._stream_round.await_count == 3


@pytest.mark.asyncio
async def test_execution_and_checkpoint_ids_do_not_hide_identical_failures(monkeypatch):
    from uuid import uuid4
    import json
    ctx, node = make_node()
    async def failure(*args):
        return json.dumps({"ok": False, "error": "same failure", "execution_id": uuid4().hex, "checkpoint_id": uuid4().hex})
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", failure)
    node._stream_round = AsyncMock(return_value=ChatResult(tool_calls=[ToolCall("c", "run_python", '{"code":"broken"}')]))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.agent_run.outcome.reason == "repeated_tool_failure"
    assert node._stream_round.await_count == 3


@pytest.mark.asyncio
async def test_successful_polling_is_not_treated_as_failure_loop(monkeypatch):
    ctx, node = make_node()
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", AsyncMock(return_value='{"ok":true,"status":"running"}'))
    node._stream_round = AsyncMock(side_effect=[*[ChatResult(tool_calls=[ToolCall(str(i), "poll", "{}")]) for i in range(5)], ChatResult(content="done")])
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.agent_run.step == 6


@pytest.mark.asyncio
async def test_stop_has_priority_over_continue():
    ctx, node = make_node()
    node._stop_policies = (
        SimpleNamespace(on_stop=AsyncMock(return_value=StopDecision(StopAction.CONTINUE, "more work"))),
        SimpleNamespace(on_stop=AsyncMock(return_value=StopDecision(StopAction.STOP, "not allowed"))),
    )
    node._stream_round = AsyncMock(return_value=ChatResult(content="draft"))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.stop_reason == "not allowed"
    assert node._stream_round.await_count == 1
