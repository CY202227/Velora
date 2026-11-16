import gzip
import json

import pytest

from server.core.agent.maintenance import RuntimeArchive
from server.core.agent.state import RunState, RunPhase, RunStatus
from server.core.agent.store import RunStore
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.conversation.store import ConversationStore
from server.core.execution.artifacts import OutputStore
from server.core.execution.store import ExecutionStore


@pytest.fixture
async def evidence(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "runtime.db").as_posix())
    await db.init()
    try:
        yield db, OutputStore(tmp_path / "receipts"), tmp_path
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_receipt_recovers_completion_after_database_commit_loss(evidence, monkeypatch):
    db, receipts, _ = evidence
    store = ExecutionStore(db._session_factory, receipts)
    await store.claim("e", session_id="s", run_id="r", operation="run_python", digest="a" * 64, location="local", backend="native")
    await store.start("e")
    async def fail(*args, **kwargs):
        raise OSError("simulated database loss after receipt")
    monkeypatch.setattr(store, "_change", fail)
    with pytest.raises(OSError):
        await store.finish("e", '{"execution_id":"e","ok":true,"status":"succeeded","exit_code":0}')
    restarted = ExecutionStore(db._session_factory, receipts)
    assert (await restarted.read("e"))["status"] == "result_unknown"
    assert (await restarted.reconcile())["reconciled"] == 1
    assert (await restarted.read("e"))["status"] == "succeeded"
    assert not await restarted.claim("e", session_id="s", run_id="r", operation="run_python", digest="a" * 64, location="local", backend="native")


@pytest.mark.asyncio
async def test_unknown_without_receipt_remains_unknown(evidence):
    db, receipts, _ = evidence
    store = ExecutionStore(db._session_factory)
    await store.claim("e", session_id="s", run_id="r", operation="run_python", digest="a" * 64, location="local", backend="native")
    await store.start("e")
    restarted = ExecutionStore(db._session_factory, receipts)
    assert (await restarted.reconcile())["reconciled"] == 0
    assert (await restarted.read("e"))["status"] == "result_unknown"


@pytest.mark.asyncio
async def test_archive_keeps_evidence_and_nonreplayable_identity(evidence):
    db, receipts, path = evidence
    run = RunState(phase=RunPhase.REQUESTING)
    run.finish(RunStatus.COMPLETED)
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", model="m", agent_run=run)
    runs = RunStore(db._session_factory)
    await runs.checkpoint(ctx, "chain_exit")
    store = ExecutionStore(db._session_factory, receipts)
    await store.claim("e", session_id="s", run_id=run.run_id, operation="run_python", digest="a" * 64, location="local", backend="native")
    await store.start("e")
    await store.finish("e", '{"execution_id":"e","ok":true,"status":"succeeded"}')
    archive = RuntimeArchive(db._session_factory, path / "archives", 100000, receipts=receipts)
    assert await archive.prune({run.run_id}) == 1
    assert await runs.read(run.run_id) is None
    assert (await store.read("e"))["status"] == "archived"
    assert not await store.claim("e", session_id="s", run_id=run.run_id, operation="run_python", digest="a" * 64, location="local", backend="native")
    bundle = json.loads(gzip.decompress(next((path / "archives").glob("*.gz")).read_bytes()))
    assert bundle["receipts"]["e"]["result"]
    assert not receipts._path("e").exists()


@pytest.mark.asyncio
async def test_archive_budget_failure_preserves_database(evidence):
    db, _, path = evidence
    run = RunState()
    run.finish(RunStatus.COMPLETED)
    ctx = TurnContext(request=TurnRequest("s", "task"), session_id="s", agent_run=run)
    runs = RunStore(db._session_factory)
    await runs.checkpoint(ctx, "chain_exit")
    with pytest.raises(ValueError, match="budget"):
        await RuntimeArchive(db._session_factory, path / "archives", 1).prune({run.run_id})
    assert await runs.read(run.run_id) is not None


@pytest.mark.asyncio
async def test_storage_pressure_blocks_work_before_side_effect(evidence, monkeypatch):
    from server.config import Settings
    from server.core.agent.storage_budget import RuntimeStorageBudget
    from server.core.execution.service import execute
    from unittest.mock import AsyncMock
    db, _, _ = evidence
    budget = RuntimeStorageBudget(db._engine, 1, reserve_bytes=0)
    effect = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    result = json.loads(await execute(Settings(_env_file=None), "s", "run_python", {"code": "print(1)"},
        store=ExecutionStore(db._session_factory, admission=budget.check)))
    assert result["status"] == "denied" and result["reason"] == "runtime_storage_budget_exhausted"
    effect.assert_not_awaited()
