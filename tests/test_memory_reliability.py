from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from server.core.memory.client import AtomMemoryClient
from server.core.memory.consolidate import ConsolidateJob
from server.core.conversation.store import ConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {"status": "failed"}, {"status": "running"}])
async def test_failed_run_is_not_success(result):
    client = AtomMemoryClient("http://unused")
    client.consolidate = AsyncMock(return_value=result)
    client.get_source = AsyncMock()
    assert await ConsolidateJob(client).run_now("space", source_id=1) is False
    client.get_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_run_must_consume_correction():
    client = AtomMemoryClient("http://unused")
    client.consolidate = AsyncMock(return_value={"status": "succeeded"})
    client.get_source = AsyncMock(return_value={"status": "pending"})
    job = ConsolidateJob(client)
    assert await job.run_now("space", source_id=1) is False
    client.get_source.return_value = {"status": "consolidated"}
    assert await job.run_now("space", source_id=1) is True


@pytest.mark.asyncio
async def test_delivery_survives_restart_and_retries_same_key(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'delivery.db').as_posix()}"
    store = ConversationStore(url)
    await store.init()
    client = AtomMemoryClient("http://unused", delivery_store=store)
    client._send_source = AsyncMock(return_value=None)
    assert await client.add_source("space", kind="correction", content="叫我小周") is None
    key, uid, payload = (await store.pending_memory_deliveries())[0]
    await store.close()
    store = ConversationStore(url)
    await store.init()
    client = AtomMemoryClient("http://unused", delivery_store=store)
    client._send_source = AsyncMock(return_value={"id": 1, "status": "pending"})
    client.consolidate = AsyncMock(return_value={"status": "failed"})
    await client.retry_deliveries()
    assert len(await store.pending_memory_deliveries()) == 1
    client._send_source.assert_awaited_with(uid, payload)
    assert payload["idempotency_key"] == key
    client.consolidate.return_value = {"status": "succeeded"}
    client.get_source = AsyncMock(return_value={"status": "consolidated"})
    await client.retry_deliveries()
    assert await store.pending_memory_deliveries() == []
    await store.close()


@pytest.mark.asyncio
async def test_exchange_and_delivery_commit_together(tmp_path):
    from server.core.nodes.persist_turn import PersistTurnNode
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest
    store = ConversationStore(f"sqlite+aiosqlite:///{(tmp_path / 'atomic.db').as_posix()}")
    await store.init()
    session = await store.create_session(persona_id="test", memory_space_uid="space", model="fake")
    client = AtomMemoryClient("http://unused", delivery_store=store)
    # Simulate abrupt cancellation immediately before the first HTTP request.
    import asyncio
    client.add_source = AsyncMock(side_effect=asyncio.CancelledError)
    ctx = TurnContext(request=TurnRequest(session.id, "请记住我喜欢茶"), session_id=session.id,
                      memory_space_uid="space", assistant_text="好的")
    with pytest.raises(asyncio.CancelledError):
        await PersistTurnNode(store, client, ConsolidateJob(client)).process(ctx)
    turns = await store.list_turns(session.id)
    pending = await store.pending_memory_deliveries()
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert len(pending) == 1
    assert pending[0][0] == turns[1].id
    assert pending[0][2]["external_ref"]["session_id"] == session.id
    assert "喜欢茶" in pending[0][2]["content"]
    await store.close()


@pytest.mark.asyncio
async def test_exchange_rolls_back_when_delivery_cannot_be_saved(tmp_path):
    from sqlalchemy import event
    store = ConversationStore(f"sqlite+aiosqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    await store.init()
    session = await store.create_session(persona_id="test", memory_space_uid="space", model="fake")
    def reject(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO memory_deliveries"):
            raise RuntimeError("disk failure")
    event.listen(store._engine.sync_engine, "before_cursor_execute", reject)
    with pytest.raises(RuntimeError, match="disk failure"):
        await store.save_exchange(session.id, "请记住", "好的", memory_uid="space",
                                  memory_payload={"kind": "turn", "content": "fact", "salience": 0.2})
    event.remove(store._engine.sync_engine, "before_cursor_execute", reject)
    assert await store.list_turns(session.id) == []
    assert await store.pending_memory_deliveries() == []
    await store.close()


@pytest.mark.asyncio
async def test_retries_consolidate_each_space_once(tmp_path):
    store = ConversationStore(f"sqlite+aiosqlite:///{(tmp_path / 'batch.db').as_posix()}")
    await store.init()
    for i in range(3):
        await store.save_memory_delivery(str(i), "space", {"kind": "turn", "content": str(i), "idempotency_key": str(i)})
    client = AtomMemoryClient("http://unused", delivery_store=store)
    client._send_source = AsyncMock(return_value={"id": 1, "status": "pending"})
    client.get_source = AsyncMock(return_value={"status": "consolidated"})
    job = AsyncMock()
    job.run_now.return_value = True
    await client.retry_deliveries(job)
    job.run_now.assert_awaited_once_with("space", trigger="scheduled")
    assert await store.pending_memory_deliveries() == []
    await store.close()
