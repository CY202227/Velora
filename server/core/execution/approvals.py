"""Persistent, request-bound single-use decisions; no implicit permission grants."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import asyncio

from sqlalchemy import String, update
from sqlalchemy.orm import Mapped, mapped_column

from server.core.conversation.store import Base


def now():
    return datetime.now(timezone.utc).isoformat()


class ApprovalRecord(Base):
    __tablename__ = "execution_approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[str] = mapped_column(String(40))
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    consumed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ApprovalStore:
    def __init__(self, sessions):
        self._sessions = sessions

    async def request(self, *, execution_id, session_id, digest, target, ttl_seconds=300):
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("approval lifetime must be between 1 and 3600 seconds")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("approval requires a SHA-256 request digest")
        identifier = uuid4().hex
        async with self._sessions() as db:
            db.add(ApprovalRecord(id=identifier, execution_id=execution_id, session_id=session_id,
                                  digest=digest, target=target, status="pending",
                                  expires_at=(datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()))
            await db.commit()
        return identifier

    async def decide(self, identifier, *, session_id, approve: bool):
        if type(approve) is not bool:
            raise ValueError("approval decision must be boolean")
        async with self._sessions() as db:
            changed = await db.execute(update(ApprovalRecord).where(
                ApprovalRecord.id == identifier, ApprovalRecord.session_id == session_id,
                ApprovalRecord.status == "pending", ApprovalRecord.expires_at > now(),
            ).values(status="approved" if approve else "denied", decided_at=now()))
            if changed.rowcount != 1:
                raise ValueError("approval missing, expired, decided, or belongs to another session")
            await db.commit()

    async def consume(self, identifier, *, execution_id, session_id, digest, target):
        async with self._sessions() as db:
            changed = await db.execute(update(ApprovalRecord).where(
                ApprovalRecord.id == identifier, ApprovalRecord.execution_id == execution_id,
                ApprovalRecord.session_id == session_id, ApprovalRecord.digest == digest,
                ApprovalRecord.target == target, ApprovalRecord.status == "approved",
                ApprovalRecord.expires_at > now(),
            ).values(status="consumed", consumed_at=now()))
            if changed.rowcount != 1:
                raise ValueError("approval invalid, expired, mismatched, or already consumed")
            await db.commit()

    async def read(self, identifier, *, session_id):
        async with self._sessions() as db:
            row = await db.get(ApprovalRecord, identifier)
            if row is None or row.session_id != session_id:
                return None
            status = row.status
            if status in ("pending", "approved") and row.expires_at <= now():
                status = "expired"
            return {"approval_id": row.id, "execution_id": row.execution_id,
                    "target": row.target, "status": status, "expires_at": row.expires_at}

    async def abandon(self, identifier):
        async with self._sessions() as db:
            await db.execute(update(ApprovalRecord).where(
                ApprovalRecord.id == identifier, ApprovalRecord.status.in_(("pending", "approved")),
                ApprovalRecord.expires_at > now(),
            ).values(status="cancelled"))
            await db.commit()


class ApprovalCoordinator:
    """Wait in the existing tool task; never start a second execution driver."""
    def __init__(self, store):
        self.store = store
        self.pending = {}

    async def wait(self, *, execution_id, session_id, digest, target, publish, ttl_seconds=300):
        identifier = await self.store.request(execution_id=execution_id, session_id=session_id,
                                               digest=digest, target=target, ttl_seconds=ttl_seconds)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = (session_id, future)
        handed_off = False
        try:
            await publish(identifier)
            try:
                accepted = await asyncio.wait_for(future, ttl_seconds)
            except asyncio.TimeoutError as exc:
                raise PermissionError("approval expired; workload not started") from exc
            if not accepted:
                raise PermissionError("approval denied; workload not started")
            handed_off = True
            return identifier
        finally:
            self.pending.pop(identifier, None)
            if not handed_off:
                await asyncio.shield(self.store.abandon(identifier))

    async def decide(self, identifier, *, session_id, approve):
        waiting = self.pending.get(identifier)
        if waiting is None or waiting[0] != session_id or waiting[1].done():
            raise ValueError("approval has no live waiting tool; it cannot resume a restarted task")
        await self.store.decide(identifier, session_id=session_id, approve=approve)
        if not waiting[1].done():
            waiting[1].set_result(approve)
