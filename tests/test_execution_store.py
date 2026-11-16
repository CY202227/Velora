import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from server.config import Settings
from server.core.conversation.store import ConversationStore
from server.core.execution.service import execute
from server.core.execution.store import ExecutionStore


@pytest.fixture
async def setup(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "execution.db").as_posix())
    await db.init()
    store = ExecutionStore(db._session_factory)
    settings = Settings(_env_file=None, computer_enabled=True, workspaces_dir=str(tmp_path / "workspaces"))
    try:
        yield settings, store, db
    finally:
        await db.close()


async def submit(settings, store, **kwargs):
    return json.loads(await execute(settings, "s", "run_python", {"code": "synthetic"},
                                    execution_id="execution-1", run_id="run-1", store=store, **kwargs))


@pytest.mark.asyncio
async def test_durable_claim_precedes_effect_and_duplicate_returns_result(setup, monkeypatch):
    settings, store, db = setup
    async def effect(*args, **kwargs):
        assert (await store.read("execution-1"))["status"] == "running"
        return json.dumps({"execution_id": "execution-1", "ok": True, "stdout": "done"})
    mock = AsyncMock(side_effect=effect)
    monkeypatch.setattr("server.core.execution.service._execute", mock)
    first = await submit(settings, store)
    assert await submit(settings, store) == first
    restarted = ExecutionStore(db._session_factory)
    assert await submit(settings, restarted) == first
    mock.assert_awaited_once()
    assert "result" not in (await restarted.read("execution-1"))


@pytest.mark.asyncio
async def test_same_id_different_request_rejected(setup, monkeypatch):
    settings, store, _ = setup
    mock = AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr("server.core.execution.service._execute", mock)
    await submit(settings, store)
    result = json.loads(await execute(settings, "s", "run_python", {"code": "different"},
                                      execution_id="execution-1", run_id="run-1", store=store))
    assert result["reason"] == "execution_conflict"
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_submission_runs_once(setup, monkeypatch):
    settings, store, _ = setup
    started, release = asyncio.Event(), asyncio.Event()
    async def effect(*args, **kwargs):
        started.set()
        await release.wait()
        return '{"ok":true}'
    mock = AsyncMock(side_effect=effect)
    monkeypatch.setattr("server.core.execution.service._execute", mock)
    task = asyncio.create_task(submit(settings, store))
    try:
        await asyncio.wait_for(started.wait(), 3)
        duplicate = await submit(settings, store)
        assert duplicate["reason"] == "execution_not_replayed"
        assert duplicate["status"] == "running"
    finally:
        release.set()
        await task
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_restarted_inflight_is_unknown_never_replayed(setup, monkeypatch):
    settings, store, db = setup
    started, release = asyncio.Event(), asyncio.Event()
    async def effect(*args, **kwargs):
        started.set()
        await release.wait()
        return '{"ok":true}'
    mock = AsyncMock(side_effect=effect)
    monkeypatch.setattr("server.core.execution.service._execute", mock)
    task = asyncio.create_task(submit(settings, store))
    try:
        await asyncio.wait_for(started.wait(), 3)
        result = await submit(settings, ExecutionStore(db._session_factory))
        assert result["status"] == "result_unknown"
        assert result["reason"] == "execution_not_replayed"
    finally:
        release.set()
        await task
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_failure_blocks_launch(setup, monkeypatch):
    settings, store, _ = setup
    store.claim = AsyncMock(side_effect=OSError("database offline"))
    mock = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", mock)
    assert (await submit(settings, store))["reason"] == "execution_store_unavailable"
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_commit_failure_is_unknown_and_not_replayed(setup, monkeypatch):
    settings, store, _ = setup
    store.finish = AsyncMock(side_effect=OSError("database offline"))
    mock = AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr("server.core.execution.service._execute", mock)
    assert (await submit(settings, store))["reason"] == "execution_result_uncommitted"
    assert (await store.read("execution-1"))["status"] == "result_unknown"
    assert (await submit(settings, store))["reason"] == "execution_not_replayed"
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_durably_records_unknown(setup, monkeypatch):
    settings, store, _ = setup
    started = asyncio.Event()
    async def effect(*args, **kwargs):
        started.set()
        await asyncio.Future()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    task = asyncio.create_task(submit(settings, store))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.read("execution-1"))["status"] == "result_unknown"
    assert not store.active


@pytest.mark.asyncio
async def test_provider_key_is_redacted_in_durable_result(setup, monkeypatch):
    settings, store, _ = setup
    monkeypatch.setattr("server.core.execution.service._execute", AsyncMock(return_value='{"ok":true,"stdout":"synthetic-key"}'))
    result = await submit(settings, store, redact=("synthetic-key",))
    assert result["stdout"] == "[redacted]"
    assert "synthetic-key" not in (await store.read("execution-1", include_result=True))["result"]


@pytest.mark.asyncio
async def test_unknown_tool_result_stops_loop_before_next_model_request(monkeypatch):
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest, NodeResult
    from server.core.nodes.llm import LLMNode
    from server.core.provider.base import ChatResult, ToolCall
    from server.core.tools.registry import ToolRegistry
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", llm_base_url="http://localhost")
    ctx.extras["tools"] = [{"type": "function"}]
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(tool_calls=[ToolCall("a", "run_python", "{}")]))
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", AsyncMock(return_value='{"ok":false,"status":"result_unknown"}'))
    assert await node.process(ctx) is NodeResult.STOP
    assert ctx.stop_reason == "execution_result_unknown"
    assert ctx.agent_run.tool_executions[0]["status"] == "result_unknown"
    assert ctx.agent_run.tool_executions[0]["execution_id"]
    node._stream_round.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_id_is_checkpointed_before_dispatch(monkeypatch):
    from server.core.agent.state import RunState
    from server.core.agent.tool_round import execute_tool_round
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest
    from server.core.provider.base import ChatResult, ToolCall
    from server.core.tools.registry import ToolRegistry
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", agent_run=RunState())
    captured = []
    async def checkpoint(boundary):
        if boundary == "tool_started":
            captured.append(ctx.agent_run.tool_executions[0]["execution_id"])
    ctx.checkpoint = checkpoint
    async def handler(*args):
        assert captured == [ctx.extras["execution_id"]]
        return '{"ok":true}'
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", handler)
    await execute_tool_round(ctx, ToolRegistry(), [], ChatResult(tool_calls=[ToolCall("a", "run_python", "{}")]), 0)
    assert "execution_id" not in ctx.extras


@pytest.mark.asyncio
async def test_request_scoped_ids_do_not_defeat_repeated_failure_guard(monkeypatch):
    from server.core.agent.state import RunState
    from server.core.agent.tool_round import execute_tool_round
    from server.core.agent.policy import RepeatedToolFailure
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest
    from server.core.provider.base import ChatResult, ToolCall
    from server.core.tools.registry import ToolRegistry
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", agent_run=RunState())
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", AsyncMock(side_effect=[
        json.dumps({"ok": False, "error": "same failure", "execution_id": str(i)}) for i in range(3)
    ]))
    for i in range(2):
        await execute_tool_round(ctx, ToolRegistry(), [], ChatResult(tool_calls=[ToolCall(str(i), "run_python", "{}")]), i, failure_limit=3)
    with pytest.raises(RepeatedToolFailure):
        await execute_tool_round(ctx, ToolRegistry(), [], ChatResult(tool_calls=[ToolCall("3", "run_python", "{}")]), 2, failure_limit=3)


@pytest.mark.asyncio
async def test_execution_api_returns_metadata_without_private_output(setup, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from server.api.debug import router
    settings, store, _ = setup
    monkeypatch.setattr("server.core.execution.service._execute", AsyncMock(return_value='{"ok":true,"stdout":"private-workspace-content"}'))
    await submit(settings, store)
    app = FastAPI()
    app.include_router(router)
    app.state.velora = SimpleNamespace(router=SimpleNamespace(execution_store=store))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/debug/executions/execution-1")
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert "private-workspace-content" not in response.text
        assert "result" not in response.json()
        assert (await client.get("/api/debug/executions?session_id=other")).json() == {"executions": []}
        assert (await client.get("/api/debug/executions/missing")).status_code == 404
