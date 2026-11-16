import asyncio

import pytest
from sqlalchemy import update

from server.core.conversation.store import ConversationStore
from server.core.execution.approvals import ApprovalStore, ApprovalRecord


@pytest.fixture
async def approvals(tmp_path):
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "approvals.db").as_posix())
    await db.init()
    try:
        yield ApprovalStore(db._session_factory), db
    finally:
        await db.close()


def bound():
    return dict(execution_id="e", session_id="s", digest="a" * 64, target="local-device")


@pytest.mark.asyncio
async def test_only_one_concurrent_consumer(approvals):
    store, _ = approvals
    identifier = await store.request(**bound())
    await store.decide(identifier, session_id="s", approve=True)
    results = await asyncio.gather(store.consume(identifier, **bound()), store.consume(identifier, **bound()), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert (await store.read(identifier, session_id="s"))["status"] == "consumed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("execution_id", "other"), ("session_id", "other"), ("digest", "b" * 64), ("target", "server")])
async def test_mismatched_scope_does_not_spend_grant(approvals, field, value):
    store, _ = approvals
    identifier = await store.request(**bound())
    await store.decide(identifier, session_id="s", approve=True)
    with pytest.raises(ValueError):
        await store.consume(identifier, **{**bound(), field: value})
    await store.consume(identifier, **bound())


@pytest.mark.asyncio
async def test_pending_denied_and_foreign_session_cannot_grant(approvals):
    store, _ = approvals
    identifier = await store.request(**bound())
    with pytest.raises(ValueError):
        await store.consume(identifier, **bound())
    assert await store.read(identifier, session_id="foreign") is None
    with pytest.raises(ValueError):
        await store.decide(identifier, session_id="foreign", approve=True)
    await store.decide(identifier, session_id="s", approve=False)
    with pytest.raises(ValueError):
        await store.decide(identifier, session_id="s", approve=True)
    with pytest.raises(ValueError):
        await store.consume(identifier, **bound())


@pytest.mark.asyncio
async def test_expired_grant_and_restart(approvals):
    store, db = approvals
    identifier = await store.request(**bound())
    await store.decide(identifier, session_id="s", approve=True)
    async with db._session_factory() as session:
        await session.execute(update(ApprovalRecord).where(ApprovalRecord.id == identifier).values(expires_at="2000-01-01T00:00:00+00:00"))
        await session.commit()
    restarted = ApprovalStore(db._session_factory)
    assert (await restarted.read(identifier, session_id="s"))["status"] == "expired"
    with pytest.raises(ValueError):
        await restarted.consume(identifier, **bound())
