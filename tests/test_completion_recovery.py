import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from server.core.agent.acceptance import AcceptanceCheck, AcceptancePolicy
from server.core.agent.policy import StopAction
from server.core.agent.state import RunState
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest, NodeResult
from server.core.execution.snapshots import SnapshotService
from server.core.nodes.llm import LLMNode
from server.core.provider.base import ChatResult, ToolCall
from server.core.tools.registry import ToolRegistry


def make_context():
    return TurnContext(request=TurnRequest("s", "task"), session_id="s", llm_base_url="http://localhost",
                       messages=[{"role": "user", "content": "task"}])


@pytest.mark.asyncio
async def test_truncated_text_continues_and_keeps_full_answer():
    ctx = make_context()
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock(side_effect=[ChatResult(content="first ", finish_reason="length"), ChatResult(content="last", finish_reason="stop")])
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.assistant_text == "first last"
    assert ctx.agent_run.output_recoveries == 1


@pytest.mark.asyncio
async def test_truncated_tool_is_not_dispatched(monkeypatch):
    ctx = make_context()
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(side_effect=[ChatResult(tool_calls=[ToolCall("broken", "run_shell", '{"command":')], finish_reason="length"), ChatResult(content="could not run")])
    dispatch = AsyncMock()
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", dispatch)
    assert await node.process(ctx) is NodeResult.CONTINUE
    dispatch.assert_not_awaited()
    assert not ctx.agent_run.tool_executions


@pytest.mark.asyncio
async def test_repeated_output_cutoff_stops_without_success():
    ctx = make_context()
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(content="partial", finish_reason="length"))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.stop_reason == "output_truncated" and ctx.assistant_text == ""
    assert node._stream_round.await_count == 4


@pytest.mark.asyncio
async def test_empty_continuation_cannot_promote_partial_text_to_success():
    ctx = make_context()
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock(side_effect=[ChatResult(content="unfinished", finish_reason="length"), ChatResult(content="", finish_reason="stop")])
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.stop_reason == "output_truncated" and ctx.assistant_text == ""


@pytest.mark.asyncio
async def test_provider_overflow_prunes_then_retries_without_tools(monkeypatch):
    ctx = make_context()
    ctx.messages = [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [{"id": "old"}]},
                    {"role": "tool", "tool_call_id": "old", "content": "x" * 14000},
                    {"role": "assistant", "tool_calls": [{"id": "new"}]}, {"role": "tool", "tool_call_id": "new", "content": "evidence"}]
    response = httpx.Response(400, json={"error": {"code": "context_length_exceeded"}}, request=httpx.Request("POST", "http://local/v1"))
    error = httpx.HTTPStatusError("overflow", request=response.request, response=response)
    node = LLMNode(object(), context_window=32000)
    node._stream_round = AsyncMock(side_effect=[error, ChatResult(content="done")])
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.agent_run.context_recoveries == 1 and ctx.agent_run.pruned_results == 1
    assert ctx.messages[-1]["content"] == "evidence"


@pytest.mark.asyncio
async def test_file_acceptance_checks_real_content_and_rejects_missing(tmp_path):
    ctx = make_context()
    ctx.agent_run = RunState(acceptance=[{"kind": "file", "path": "result.json", "contains": "ready", "json_valid": True}])
    root = tmp_path / "workspaces/s"
    root.mkdir(parents=True)
    ctx.extras.update(settings=SimpleNamespace(workspaces_dir=str(root.parent)), snapshots=SnapshotService(tmp_path / "private"))
    policy = AcceptancePolicy()
    assert (await policy.on_stop(ctx, "done")).action is StopAction.CONTINUE
    (root / "result.json").write_text('{"state":"ready"}')
    assert (await policy.on_stop(ctx, "done")).action is StopAction.FINISH
    (root / "result.json").write_text('ready but not JSON')
    assert (await policy.on_stop(ctx, "done")).action is StopAction.CONTINUE
    assert ctx.agent_run.verification["status"] == "failed"


@pytest.mark.asyncio
async def test_test_command_requires_exit_evidence_after_last_write():
    ctx = make_context()
    ctx.agent_run = RunState(acceptance=[{"kind": "command", "source": "pytest"}], tool_executions=[
        {"name": "run_shell", "source_digest": hashlib.sha256(b"pytest").hexdigest(), "status": "returned", "result": '{"ok":true,"exit_code":0}'}])
    policy = AcceptancePolicy()
    assert (await policy.on_stop(ctx, "tests passed")).action is StopAction.FINISH
    ctx.agent_run.tool_executions.append({"name": "write_file"})
    assert (await policy.on_stop(ctx, "tests passed")).action is StopAction.CONTINUE


@pytest.mark.asyncio
async def test_readonly_shell_verification_does_not_invalidate_completed_tests():
    ctx = make_context()
    ctx.agent_run = RunState(acceptance=[{"kind": "command", "source": "pytest"}], tool_executions=[
        {"name": "run_shell", "source_digest": hashlib.sha256(b"pytest").hexdigest(), "status": "returned", "result": '{"ok":true,"exit_code":0}'},
        {"name": "run_python", "status": "returned", "workspace_changed": False, "result": '{"ok":true,"exit_code":0}'}])
    policy = AcceptancePolicy()
    assert (await policy.on_stop(ctx, "tests passed")).action is StopAction.FINISH
    ctx.agent_run.tool_executions[-1]["workspace_changed"] = True
    assert (await policy.on_stop(ctx, "tests passed")).action is StopAction.CONTINUE


@pytest.mark.parametrize("path", ["../outside", "C:/outside", "/outside", ".env", ".git/config"])
def test_acceptance_cannot_read_outside_or_private_files(path):
    with pytest.raises(ValueError):
        AcceptanceCheck(kind="file", path=path)


@pytest.mark.asyncio
async def test_failed_acceptance_cannot_be_reported_as_success():
    ctx = make_context()
    ctx.request.client_meta["acceptance"] = [{"kind": "file", "path": "missing.json"}]
    node = LLMNode(object(), context_window=32000, stop_policies=(AcceptancePolicy(),))
    node._stream_round = AsyncMock(return_value=ChatResult(content="I completed it"))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.assistant_text == "" and ctx.agent_run.verification["status"] == "failed"


@pytest.mark.asyncio
async def test_restart_preserves_partial_text_and_next_step(tmp_path):
    from server.core.conversation.store import ConversationStore
    from server.core.agent.store import RunStore
    from server.core.agent.state import RunPhase
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "runtime.db").as_posix())
    await db.init()
    try:
        ctx = make_context()
        ctx.agent_run = RunState(phase=RunPhase.REQUESTING, step=1, answer_prefix="first ", output_recoveries=1,
                                messages=[{"role": "user", "content": "task"}, {"role": "assistant", "content": "first "}, {"role": "user", "content": "continue"}])
        runs = RunStore(db._session_factory)
        await runs.checkpoint(ctx, "output_continuation_ready")
        resumed = make_context()
        resumed.extras["resume_checkpoint"] = await runs.claim(ctx.agent_run.run_id)
        node = LLMNode(object(), context_window=32000)
        node._stream_round = AsyncMock(return_value=ChatResult(content="last"))
        assert await node.process(resumed) is NodeResult.CONTINUE
        assert resumed.assistant_text == "first last" and resumed.agent_run.step == 2
    finally:
        await db.close()
