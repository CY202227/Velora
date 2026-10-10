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
