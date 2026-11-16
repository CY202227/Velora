import asyncio
import json
from uuid import NAMESPACE_URL, uuid5
from unittest.mock import AsyncMock

import pytest

from server.core.agent.state import RunPhase, RunState, RunStatus
from server.core.agent.store import RunStore
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest, NodeResult
from server.core.conversation.store import ConversationStore
from server.core.execution.store import ExecutionStore
from server.core.nodes.llm import LLMNode
from server.core.provider.base import ChatResult
from server.core.tools.registry import ToolRegistry


@pytest.fixture
async def database(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "recovery.db").as_posix())
    await db.init()
    try:
        yield db
    finally:
        await db.close()


async def crashed_boundary(db, *, snapshot_returned=False, extra_pending=False, saved_status="succeeded", wrong_run=False):
    run = RunState(phase=RunPhase.EXECUTING_TOOLS, step=1, context_window=32000)
    call = {"id": "call", "type": "function", "function": {"name": "run_python", "arguments": '{"code":"print(1)"}'}}
    run.messages = [{"role": "system", "content": "preserve this"}, {"role": "user", "content": "task"},
                    {"role": "assistant", "tool_calls": [call], "content": None}]
    execution_id = uuid5(NAMESPACE_URL, f"velora:{run.run_id}:0:0:call").hex
    output = json.dumps({"ok": saved_status == "succeeded", "status": saved_status, "execution_id": execution_id, "stdout": "1"})
    run.tool_executions = [{"id": "call", "name": "run_python", "round": 0,
                            "execution_id": execution_id, "status": "returned" if snapshot_returned else "running"}]
    if snapshot_returned:
        run.tool_executions[0]["result"] = output
    if extra_pending:
        run.messages[-1]["tool_calls"].append({"id": "later", "function": {"name": "write_file", "arguments": "{}"}})
        run.tool_executions.append({"id": "later", "name": "write_file", "round": 0, "status": "not_started"})
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", model="model", agent_run=run)
    checkpoints = RunStore(db._session_factory)
    await checkpoints.checkpoint(ctx, "tool_returned" if snapshot_returned else "tool_started")
    executions = ExecutionStore(db._session_factory)
    await executions.claim(execution_id, session_id="s", run_id="other" if wrong_run else run.run_id,
                           operation="run_python", digest="a" * 64, location="local", backend="native")
    await executions.start(execution_id)
    await executions.finish(execution_id, output)
    return checkpoints, ctx, output


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_returned", [False, True])
async def test_restart_reattaches_saved_result_without_executing_tool(database, monkeypatch, snapshot_returned):
    _, ctx, output = await crashed_boundary(database, snapshot_returned=snapshot_returned)
    restarted = RunStore(database._session_factory)
    assert await restarted.recovery_status(ctx.agent_run.run_id) == {"recoverable": True, "kind": "closed_tool_batch"}
    child = await restarted.claim(ctx.agent_run.run_id)
    assert child["resume_next_step"] and child["run"]["messages"][-1] == {"role": "tool", "tool_call_id": "call", "content": output}
    assert child["run"]["messages"][0]["content"] == "preserve this"
    assert child["run"]["run_id"] != ctx.agent_run.run_id
    resumed = TurnContext(request=TurnRequest("s", "task"), session_id="s", model="model", llm_base_url="http://localhost")
    resumed.extras["resume_checkpoint"] = child
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(content="verified"))
    tool = AsyncMock()
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", tool)
    assert await node.process(resumed) is NodeResult.CONTINUE
    assert resumed.agent_run.step == 2
    tool.assert_not_awaited()
    node._stream_round.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"saved_status": "result_unknown"}, {"wrong_run": True}])
async def test_incomplete_unknown_or_wrong_execution_refuses_resume(database, options):
    store, ctx, _ = await crashed_boundary(database, **options)
    assert not (await store.recovery_status(ctx.agent_run.run_id))["recoverable"]
    with pytest.raises(ValueError):
        await store.claim(ctx.agent_run.run_id)
    assert not (await store.read(ctx.agent_run.run_id)).get("claimed_by")


@pytest.mark.asyncio
async def test_resume_pending_suffix_only_and_survive_second_restart(database, monkeypatch):
    store, ctx, output = await crashed_boundary(database, extra_pending=True)
    assert await store.recovery_status(ctx.agent_run.run_id) == {"recoverable": True, "kind": "pending_tool_batch"}
    first = await store.claim(ctx.agent_run.run_id)
    child = await store.claim(first["run"]["run_id"])
    assert child["resume_tool_batch"]
    resumed = TurnContext(request=TurnRequest("s", "task"), session_id="s", model="model", llm_base_url="http://localhost")
    resumed.extras["resume_checkpoint"] = child
    async def checkpoint(boundary):
        await store.checkpoint(resumed, boundary)
    resumed.checkpoint = checkpoint
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(content="done"))
    effect = AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", effect)
    assert await node.process(resumed) is NodeResult.CONTINUE
    effect.assert_awaited_once()
    assert effect.call_args.args[1] == "write_file"
    assert resumed.agent_run.step == 2
    results = [m for m in resumed.agent_run.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["call", "later"]
    assert results[0]["content"] == output
    saved = await store.read(child["run"]["run_id"])
    assert [r["status"] for r in saved["run"]["tool_executions"]] == ["returned", "returned"]


@pytest.mark.asyncio
async def test_pending_suffix_with_existing_receipt_is_not_dispatched(database):
    store, ctx, _ = await crashed_boundary(database, extra_pending=True)
    execution_id = uuid5(NAMESPACE_URL, f"velora:{ctx.agent_run.run_id}:0:1:later").hex
    executions = ExecutionStore(database._session_factory)
    await executions.claim(execution_id, session_id="s", run_id=ctx.agent_run.run_id,
                           operation="write_file", digest="b" * 64, location="local", backend="native")
    assert not (await store.recovery_status(ctx.agent_run.run_id))["recoverable"]
    with pytest.raises(ValueError):
        await store.claim(ctx.agent_run.run_id)


@pytest.mark.asyncio
async def test_original_owner_cannot_advance_after_pending_batch_transfer(database):
    store, ctx, _ = await crashed_boundary(database, extra_pending=True)
    await store.claim(ctx.agent_run.run_id)
    ctx.agent_run.tool_executions[-1]["status"] = "running"
    with pytest.raises(ValueError, match="ownership transferred"):
        await store.checkpoint(ctx, "tool_started")


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_prepared_execution_is_abandoned_atomically_with_old_approval(database, approved):
    from sqlalchemy import update
    from server.core.execution.store import ExecutionRecord
    from server.core.execution.approvals import ApprovalStore
    store, ctx, _ = await crashed_boundary(database, extra_pending=True)
    execution_id = ctx.agent_run.tool_executions[0]["execution_id"]
    async with database._session_factory() as db:
        await db.execute(update(ExecutionRecord).where(ExecutionRecord.id == execution_id).values(status="prepared", result=None))
        await db.commit()
    approvals = ApprovalStore(database._session_factory)
    grant = await approvals.request(execution_id=execution_id, session_id="s", digest="a" * 64, target="local")
    if approved:
        await approvals.decide(grant, session_id="s", approve=True)
    assert (await store.recovery_status(ctx.agent_run.run_id))["recoverable"]
    assert (await approvals.read(grant, session_id="s"))["status"] == ("approved" if approved else "pending")
    child = await store.claim(ctx.agent_run.run_id)
    assert child["resume_tool_batch"]
    assert json.loads(child["run"]["messages"][-1]["content"])["reason"] == "approval_interrupted"
    assert (await approvals.read(grant, session_id="s"))["status"] == "cancelled"
    execution = await ExecutionStore(database._session_factory).read(execution_id, include_result=True)
    assert execution["status"] == "denied"
    assert json.loads(execution["result"])["reason"] == "approval_interrupted"


@pytest.mark.asyncio
async def test_truly_running_execution_remains_unrecoverable(database):
    from sqlalchemy import update
    from server.core.execution.store import ExecutionRecord
    store, ctx, _ = await crashed_boundary(database, extra_pending=True)
    async with database._session_factory() as db:
        await db.execute(update(ExecutionRecord).values(status="running", result=None))
        await db.commit()
    assert not (await store.recovery_status(ctx.agent_run.run_id))["recoverable"]
    with pytest.raises(ValueError):
        await store.claim(ctx.agent_run.run_id)


@pytest.mark.asyncio
async def test_prepared_start_racing_recovery_does_not_cancel_running_work(database, monkeypatch):
    from sqlalchemy import update
    from server.core.execution.store import ExecutionRecord
    from server.core.execution.approvals import ApprovalStore
    store, ctx, _ = await crashed_boundary(database)
    execution_id = ctx.agent_run.tool_executions[0]["execution_id"]
    async with database._session_factory() as db:
        await db.execute(update(ExecutionRecord).values(status="prepared", result=None))
        await db.commit()
    approvals = ApprovalStore(database._session_factory)
    grant = await approvals.request(execution_id=execution_id, session_id="s", digest="a" * 64, target="local")
    await approvals.decide(grant, session_id="s", approve=True)
    original = store._close_saved_tool_batch
    async def start_after_read(data, **kwargs):
        result = await original(data, **kwargs)
        async with database._session_factory() as db:
            await db.execute(update(ExecutionRecord).where(ExecutionRecord.id == execution_id).values(status="running"))
            await db.commit()
        return result
    monkeypatch.setattr(store, "_close_saved_tool_batch", start_after_read)
    with pytest.raises(ValueError, match="changed during recovery"):
        await store.claim(ctx.agent_run.run_id)
    assert not (await store.read(ctx.agent_run.run_id)).get("claimed_by")
    assert (await approvals.read(grant, session_id="s"))["status"] == "approved"


@pytest.mark.asyncio
async def test_finished_failed_run_is_not_reopened(database):
    store, ctx, _ = await crashed_boundary(database)
    ctx.agent_run.finish(RunStatus.FAILED, "cancelled")
    await store.checkpoint(ctx, "chain_exit")
    assert not (await store.recovery_status(ctx.agent_run.run_id))["recoverable"]
    with pytest.raises(ValueError):
        await store.claim(ctx.agent_run.run_id)


@pytest.mark.asyncio
async def test_competing_recovery_claims_have_one_successor(database):
    store, ctx, _ = await crashed_boundary(database)
    claims = await asyncio.gather(store.claim(ctx.agent_run.run_id), store.claim(ctx.agent_run.run_id), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in claims) == 1


@pytest.mark.asyncio
async def test_second_restart_before_request_preserves_next_step_budget(database):
    store, ctx, _ = await crashed_boundary(database)
    first = await store.claim(ctx.agent_run.run_id)
    second = await store.claim(first["run"]["run_id"])
    assert first["resume_next_step"] and second["resume_next_step"]
    assert second["run"]["step"] == 1


@pytest.mark.asyncio
async def test_mismatching_message_result_cannot_be_repaired_silently(database):
    store, ctx, _ = await crashed_boundary(database)
    ctx.agent_run.messages.append({"role": "tool", "tool_call_id": "call", "content": "different result"})
    await store.checkpoint(ctx, "mismatch")
    assert not (await store.recovery_status(ctx.agent_run.run_id))["recoverable"]


@pytest.mark.asyncio
async def test_streamed_resume_is_managed_and_reconnectable(database, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from server.api.chat import router as api_router
    from server.config import Settings
    from server.core.agent.tasks import TaskService
    from server.core.chain.router import TurnRouter
    from server.core.chain.locks import SessionLockManager
    from server.core.chain.wait_registry import WaitRegistry
    from server.terminal import events
    checkpoints, source, _ = await crashed_boundary(database)
    node = LLMNode(object(), tools=ToolRegistry(), context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(content="recovered"))
    tool = AsyncMock()
    monkeypatch.setattr("server.core.agent.tool_round.execute_tool", tool)
    async def chain(ctx, **kwargs):
        ctx.model, ctx.llm_base_url = "model", "http://localhost"
        await node.process(ctx)
        return ctx
    turn_router = TurnRouter(SimpleNamespace(execute=chain), SessionLockManager(), WaitRegistry(), run_store=checkpoints)
    settings = Settings(_env_file=None, llm_model="model")
    tasks = TaskService(database._session_factory, turn_router, settings)
    app = FastAPI()
    app.include_router(api_router)
    app.state.velora = SimpleNamespace(router=turn_router, tasks=tasks, settings=settings,
                                      store=SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(model="model"))))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/chat/resume/stream", json={"run_id": source.agent_run.run_id})
            assert response.status_code == 200, response.text
            frames = list(events(response.text.splitlines()))
            task_id = frames[0][1]["task_id"]
            assert frames[-1][1]["result"]["status"] == "completed"
            assert (await tasks.read(task_id))["status"] == "completed"
            assert frames[0][1]["resume_of"] == source.agent_run.run_id
            assert (await client.post("/api/chat/resume/stream", json={"run_id": source.agent_run.run_id})).status_code == 409
        node._stream_round.assert_awaited_once()
        tool.assert_not_awaited()
    finally:
        await tasks.close()
