import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.api.chat import router
from server.config import Settings
from server.core.agent.state import RunPhase
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest, NodeResult
from server.core.computer.tools import register_computer_tools
from server.core.conversation.store import ConversationStore
from server.core.execution.approvals import ApprovalCoordinator, ApprovalRecord, ApprovalStore
from server.core.execution.service import execute
from server.core.execution.store import ExecutionStore
from server.core.nodes.llm import LLMNode
from server.core.provider.base import ChatResult, ToolCall
from server.core.tools.registry import ToolRegistry


@pytest.fixture
async def setup(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "flow.db").as_posix())
    await db.init()
    approvals = ApprovalStore(db._session_factory)
    coordinator = ApprovalCoordinator(approvals)
    store = ExecutionStore(db._session_factory)
    settings = Settings(_env_file=None, computer_enabled=True, execution_require_approval=True,
                        execution_snapshots_enabled=False,
                        workspaces_dir=str(tmp_path / "workspaces"))
    app = FastAPI()
    app.include_router(router)
    app.state.velora = SimpleNamespace(router=SimpleNamespace(approvals=coordinator))
    try:
        yield db, approvals, coordinator, store, settings, app
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_exact_tool_batch_pauses_and_resumes_without_new_model_request(setup, monkeypatch):
    db, approvals, coordinator, store, settings, app = setup
    registry = ToolRegistry()
    register_computer_tools(registry)
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", llm_base_url="http://localhost")
    ctx.extras.update(settings=settings, execution_store=store, approvals=coordinator, tools=registry.openai_tools())
    queue = asyncio.Queue()
    async def emit(event):
        if event.type == "approval_required":
            await queue.put(event.data)
    ctx.emit = emit
    checkpoints = []
    async def checkpoint(boundary):
        checkpoints.append((boundary, ctx.agent_run.phase))
    ctx.checkpoint = checkpoint
    effects = []
    async def effect(*args, **kwargs):
        records = await store.recent()
        assert any(row["status"] == "running" for row in records)
        async with db._session_factory() as session:
            row = await session.get(ApprovalRecord, effects_approval[0])
            assert row.status == "consumed"
        effects.append(args[3])
        return json.dumps({"ok": True, "execution_id": kwargs["execution_id"]})
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    node = LLMNode(object(), tools=registry, context_window=32000)
    node._stream_round = AsyncMock(side_effect=[
        ChatResult(tool_calls=[ToolCall("first", "write_file", '{"path":"one","content":"1"}'),
                               ToolCall("second", "write_file", '{"path":"two","content":"2"}')]),
        ChatResult(content="one operation approved, one denied"),
    ])
    task = asyncio.create_task(node.process(ctx))
    effects_approval = []
    try:
        first = await asyncio.wait_for(queue.get(), 3)
        assert ctx.agent_run.phase is RunPhase.AWAITING_APPROVAL
        assert not effects and node._stream_round.await_count == 1
        effects_approval.append(first["approval_id"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 42)), base_url="http://test") as client:
            response = await client.post(f"/api/chat/approvals/{first['approval_id']}/decision", json={"session_id": "s", "approve": True})
            assert response.status_code == 200
            second = await asyncio.wait_for(queue.get(), 3)
            assert len(effects) == 1 and node._stream_round.await_count == 1
            response = await client.post(f"/api/chat/approvals/{second['approval_id']}/decision", json={"session_id": "s", "approve": False})
            assert response.status_code == 200
        assert await task is NodeResult.CONTINUE
        assert len(effects) == 1 and node._stream_round.await_count == 2
        assert any(phase is RunPhase.AWAITING_APPROVAL for _, phase in checkpoints)
        assert not coordinator.pending
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_grant_cannot_bypass_confirmation(setup, monkeypatch):
    _, _, _, store, settings, _ = setup
    effect = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    result = json.loads(await execute(settings, "s", "run_python", {"code": "x"}, store=store,
                                      request_approval=AsyncMock(return_value=None)))
    assert result["reason"] == "approval_denied"
    effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_without_interactive_channel_does_not_wait_or_execute(setup, monkeypatch):
    from server.core.agent.state import RunState
    _, _, coordinator, store, settings, _ = setup
    registry = ToolRegistry()
    register_computer_tools(registry)
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", agent_run=RunState())
    ctx.extras.update(settings=settings, execution_store=store, approvals=coordinator, execution_id="background")
    effect = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    result = json.loads(await registry.get("run_python").handler({"code": "print(1)"}, ctx))
    assert result["reason"] == "approval_denied"
    assert not coordinator.pending
    effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_and_start_are_one_transaction(setup):
    _, approvals, _, store, _, _ = setup
    await store.claim("e", session_id="s", run_id="r", operation="run_python", digest="a" * 64, location="local", backend="native")
    identifier = await approvals.request(execution_id="e", session_id="s", digest="b" * 64, target="local")
    await approvals.decide(identifier, session_id="s", approve=True)
    with pytest.raises(PermissionError):
        await store.start("e", approval_id=identifier)
    assert (await store.read("e"))["status"] == "prepared"
    assert (await approvals.read(identifier, session_id="s"))["status"] == "approved"


@pytest.mark.asyncio
async def test_expiry_and_cancellation_remove_live_waiter(setup):
    _, approvals, coordinator, _, _, _ = setup
    seen = []
    async def publish(identifier):
        seen.append(identifier)
    with pytest.raises(PermissionError, match="expired"):
        await coordinator.wait(execution_id="e", session_id="s", digest="a" * 64, target="local", publish=publish, ttl_seconds=1)
    assert not coordinator.pending
    assert (await approvals.read(seen[0], session_id="s"))["status"] == "expired"
    with pytest.raises(ValueError):
        await coordinator.decide(seen[0], session_id="s", approve=True)
    notified = asyncio.Event()
    async def publish_cancel(identifier):
        seen.append(identifier)
        notified.set()
    task = asyncio.create_task(coordinator.wait(execution_id="cancel", session_id="s", digest="a" * 64,
                                                target="local", publish=publish_cancel))
    await asyncio.wait_for(notified.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.pending
    assert (await approvals.read(seen[-1], session_id="s"))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("host,origin,status", [("remote", None, 403), ("127.0.0.1", "http://site", 403), ("127.0.0.1", None, 409)])
async def test_remote_browser_or_stale_approval_rejected(setup, host, origin, status):
    *_, app = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 42)), base_url="http://test") as client:
        response = await client.post("/api/chat/approvals/stale/decision", json={"session_id": "s", "approve": True}, headers={"Origin": origin} if origin else {})
        assert response.status_code == status
