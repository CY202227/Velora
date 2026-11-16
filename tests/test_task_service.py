import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.api.chat import router as chat_router
from server.config import Settings
from server.core.agent.tasks import TaskService
from server.core.chain.types import StreamEvent, TurnRequest
from server.core.conversation.store import ConversationStore
from server.core.execution.approvals import ApprovalCoordinator, ApprovalStore


@pytest.fixture
async def db(tmp_path):
    database = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "tasks.db").as_posix())
    await database.init()
    try:
        yield database
    finally:
        await database.close()


def service(db, handle):
    return TaskService(db._session_factory, SimpleNamespace(handle=handle), Settings(_env_file=None))


@pytest.mark.asyncio
async def test_detach_does_not_cancel_and_replay_does_not_restart(db):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handle(request, emit):
        calls.append(request.user_text)
        await emit(StreamEvent("token", {"text": "first"}))
        entered.set()
        await release.wait()
        await emit(StreamEvent("turn_end", {"assistant_text": "done", "result": {"status": "completed"}}))
    tasks = service(db, handle)
    identifier = await tasks.submit(TurnRequest("s", "task"))
    task = tasks.active[identifier]
    try:
        await asyncio.wait_for(entered.wait(), 3)
        subscriber = tasks.watch(identifier)
        first = await anext(subscriber)
        assert first["event"] == "task_started"
        await subscriber.aclose()
        assert not task.done()
        with pytest.raises(ValueError):
            await tasks.submit(TurnRequest("s", "do not duplicate"))
        release.set()
        await task
        replay = [event async for event in tasks.watch(identifier, after=1)]
        assert [event["event"] for event in replay] == ["token", "turn_end"]
        assert [json.loads(event["data"])["cursor"] for event in replay] == [2, 3]
        assert calls == ["task"]
        assert (await tasks.read(identifier))["status"] == "completed"
        assert not tasks.busy
    finally:
        await tasks.close()


@pytest.mark.asyncio
async def test_disconnected_approval_can_continue_same_task(db):
    coordinator = ApprovalCoordinator(ApprovalStore(db._session_factory))
    issued = asyncio.Queue()
    effects = []
    async def handle(request, emit):
        async def publish(identifier):
            await emit(StreamEvent("approval_required", {"approval_id": identifier}))
            await issued.put(identifier)
        await coordinator.wait(execution_id="e", session_id="s", digest="a" * 64,
                               target="local", publish=publish)
        effects.append("once")
        await emit(StreamEvent("turn_end", {"result": {"status": "completed"}}))
    tasks = service(db, handle)
    identifier = await tasks.submit(TurnRequest("s", "task"))
    task = tasks.active[identifier]
    try:
        approval = await asyncio.wait_for(issued.get(), 3)
        subscriber = tasks.watch(identifier)
        cursor = 0
        async for frame in subscriber:
            cursor = json.loads(frame["data"])["cursor"]
            if frame["event"] == "approval_required":
                break
        await subscriber.aclose()
        assert approval in coordinator.pending and not effects
        await coordinator.decide(approval, session_id="s", approve=True)
        await task
        replay = [frame async for frame in tasks.watch(identifier, after=cursor)]
        assert [frame["event"] for frame in replay] == ["turn_end"]
        assert effects == ["once"]
    finally:
        await tasks.close()


@pytest.mark.asyncio
async def test_explicit_cancel_persists_terminal_and_restart_never_replays(db):
    entered = asyncio.Event()
    async def handle(request, emit):
        entered.set()
        await asyncio.Future()
    tasks = service(db, handle)
    identifier = await tasks.submit(TurnRequest("s", "task"))
    task = tasks.active[identifier]
    try:
        await entered.wait()
        restarted = service(db, handle)
        assert (await restarted.read(identifier))["status"] == "interrupted"
        replay = [frame async for frame in restarted.watch(identifier)]
        assert replay[-1]["event"] == "task_interrupted"
        assert not restarted.active
        assert tasks.cancel(identifier)
        assert not tasks.cancel(identifier)
        await task
        assert (await tasks.read(identifier))["status"] == "cancelled"
        assert not tasks.cancel(identifier)
    finally:
        await tasks.close()


@pytest.mark.asyncio
async def test_event_storage_failure_prevents_advancing_effects(db, monkeypatch):
    effects = []
    async def handle(request, emit):
        await emit(StreamEvent("tool_call", {}))
        effects.append("must not happen")
    tasks = service(db, handle)
    append = tasks._append
    async def failing(identifier, event):
        if event.type == "tool_call":
            raise OSError("database unavailable")
        await append(identifier, event)
    monkeypatch.setattr(tasks, "_append", failing)
    identifier = await tasks.submit(TurnRequest("s", "task"))
    await tasks.active[identifier]
    assert not effects
    assert (await tasks.read(identifier))["status"] == "failed"


@pytest.mark.asyncio
async def test_task_query_api_and_cursor_validation(db):
    async def handle(request, emit):
        await emit(StreamEvent("turn_end", {"result": {"status": "completed"}}))
    tasks = service(db, handle)
    identifier = await tasks.submit(TurnRequest("s", "task"))
    task = tasks.active.get(identifier)
    if task:
        await task
    app = FastAPI()
    app.include_router(chat_router)
    app.state.velora = SimpleNamespace(tasks=tasks, store=SimpleNamespace(get_session=lambda _: None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/api/chat/tasks/{identifier}")).json()["status"] == "completed"
        assert (await client.get(f"/api/chat/tasks/{identifier}/events?after=999")).status_code == 409
        assert (await client.get(f"/api/chat/tasks/{identifier}/events?after=-1")).status_code == 422
        response = await client.get(f"/api/chat/tasks/{identifier}/events?after=1")
        assert "event: turn_end" in response.text and "task_started" not in response.text
        assert (await client.get("/api/chat/tasks/missing")).status_code == 404


@pytest.mark.asyncio
async def test_http_disconnect_only_closes_subscription(db):
    from starlette.requests import Request
    from server.api.chat import ChatBody, chat_stream
    entered, release = asyncio.Event(), asyncio.Event()
    async def handle(request, emit):
        entered.set()
        await release.wait()
        await emit(StreamEvent("turn_end", {"result": {"status": "completed"}}))
    tasks = service(db, handle)
    app = FastAPI()
    app.state.velora = SimpleNamespace(tasks=tasks, store=SimpleNamespace(get_session=AsyncMock(return_value=object())))
    scope = {"type": "http", "method": "POST", "path": "/api/chat/stream", "headers": [],
             "app": app, "asgi": {"spec_version": "2.0"}}
    response = await chat_stream(ChatBody(session_id="s", text="ordinary task"), Request(scope))
    identifier = next(iter(tasks.active))
    task = tasks.active[identifier]
    await entered.wait()
    async def disconnected():
        return {"type": "http.disconnect"}
    async def send(message):
        pass
    try:
        await response(scope, disconnected, send)
        assert not task.done(), "HTTP disconnect cancelled the execution task"
        release.set()
        await task
        assert (await tasks.read(identifier))["status"] == "completed"
    finally:
        await tasks.close()


@pytest.mark.asyncio
async def test_event_budget_stops_task_with_explicit_failure(db):
    effects = []
    async def handle(request, emit):
        await emit(StreamEvent("token", {"text": "x" * 5000}))
        effects.append("must not execute")
    tasks = service(db, handle)
    tasks.settings.task_event_budget_bytes = 4096
    identifier = await tasks.submit(TurnRequest("s", "task"))
    await tasks.active[identifier]
    snapshot = await tasks.read(identifier)
    assert snapshot["status"] == "failed"
    assert snapshot["result"]["result"]["reason"] == "event_storage_budget_exhausted"
    assert snapshot["result"]["result"]["message"]
    assert not effects


@pytest.mark.asyncio
async def test_retention_preserves_running_and_recently_finished_tasks(db):
    from sqlalchemy import update
    from server.core.agent.tasks import TaskRecord
    async def handle(request, emit):
        await emit(StreamEvent("turn_end", {"result": {"status": "completed"}}))
    tasks = service(db, handle)
    identifier = await tasks.submit(TurnRequest("s", "task"))
    await tasks.active[identifier]
    async with db._session_factory() as session:
        await session.execute(update(TaskRecord).where(TaskRecord.id == identifier).values(created_at="2000-01-01T00:00:00+00:00"))
        await session.commit()
    assert await tasks.prune() == 0
    async with db._session_factory() as session:
        await session.execute(update(TaskRecord).where(TaskRecord.id == identifier).values(result=json.dumps({"finished_at": "2000-01-01T00:00:00+00:00"})))
        await session.commit()
    assert await tasks.prune() == 1
    assert await tasks.read(identifier) is None
