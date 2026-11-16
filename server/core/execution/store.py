"""Durable execution claims and results; uncertain work is never replayed."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import String, Text, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from server.core.conversation.store import Base


class ExecutionRecord(Base):
    __tablename__ = "execution_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(16))
    backend: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    updated_at: Mapped[str] = mapped_column(String(40))
    result: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExecutionStore:
    def __init__(self, sessions, receipts=None, admission=None):
        self._sessions = sessions
        self.owner = uuid4().hex
        self.active: set[str] = set()
        self.workloads = {}
        self.receipts = receipts
        self.admission = admission

    def cancel(self, execution_id):
        task = self.workloads.get(execution_id)
        if task is None or task.done() or task.cancelling():
            return False
        task.cancel()
        return True

    async def claim(self, execution_id, *, session_id, run_id, operation, digest, location, backend) -> bool:
        if self.admission is not None:
            await self.admission()
        now = datetime.now(timezone.utc).isoformat()
        async with self._sessions() as db:
            db.add(ExecutionRecord(id=execution_id, session_id=session_id, run_id=run_id,
                                   operation=operation, digest=digest, location=location,
                                   backend=backend, owner=self.owner, status="prepared",
                                   created_at=now, updated_at=now))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                row = await db.get(ExecutionRecord, execution_id)
                if row is None:
                    raise
                if row.digest != digest or row.session_id != session_id or row.run_id != run_id:
                    raise ValueError("execution ID conflicts with a different request")
                return False
        self.active.add(execution_id)
        return True

    async def start(self, execution_id, *, approval_id=None):
        if approval_id is None:
            await self._change(execution_id, "running", None, allowed=("prepared",))
            return
        from server.core.execution.approvals import ApprovalRecord, now
        async with self._sessions() as db:
            row = await db.get(ExecutionRecord, execution_id)
            if row is None or row.owner != self.owner or row.status != "prepared":
                raise ValueError("execution ownership or state changed")
            grant = await db.execute(update(ApprovalRecord).where(
                ApprovalRecord.id == approval_id, ApprovalRecord.execution_id == execution_id,
                ApprovalRecord.session_id == row.session_id, ApprovalRecord.digest == row.digest,
                ApprovalRecord.target == row.location, ApprovalRecord.status == "approved",
                ApprovalRecord.expires_at > now(),
            ).values(status="consumed", consumed_at=now()))
            if grant.rowcount != 1:
                raise PermissionError("approval does not authorize this execution")
            changed = await db.execute(update(ExecutionRecord).where(
                ExecutionRecord.id == execution_id, ExecutionRecord.owner == self.owner,
                ExecutionRecord.status == "prepared",
            ).values(status="running", updated_at=now()))
            if changed.rowcount != 1:
                raise ValueError("execution changed while claiming approval")
            await db.commit()

    async def finish(self, execution_id, result: str):
        payload = json.loads(result)
        status = payload.get("status") or ("succeeded" if payload.get("ok") else "failed")
        if status not in ("succeeded", "failed", "denied", "result_unknown", "cancelled"):
            raise ValueError("invalid execution terminal status")
        backend = payload.get("backend")
        if self.receipts is not None and status != "result_unknown":
            async with self._sessions() as db:
                row = await db.get(ExecutionRecord, execution_id)
                if row is None or row.owner != self.owner or row.status not in ("prepared", "running"):
                    raise ValueError("execution changed before receipt commit")
                receipt = {"digest": row.digest, "run_id": row.run_id, "operation": row.operation,
                           "owner": row.owner, "result": result}
            await asyncio.to_thread(self.receipts.put, execution_id, row.session_id, receipt)
        await self._change(execution_id, status, result, allowed=("prepared", "running"),
                           backend=backend if backend in ("native", "native-windows", "container", "host-approved") else None)

    async def reconcile(self, execution_id=None):
        if self.receipts is None:
            return {"reconciled": 0, "evidence": "receipts_unavailable"}
        query = select(ExecutionRecord).where(ExecutionRecord.status.in_(("prepared", "running", "result_unknown"))).limit(500)
        if execution_id is not None:
            query = query.where(ExecutionRecord.id == execution_id)
        count = 0
        async with self._sessions() as db:
            rows = list((await db.execute(query)).scalars())
            for row in rows:
                if row.id in self.active or row.id in self.workloads:
                    continue
                try:
                    receipt = await asyncio.to_thread(self.receipts.receipt, row.id, row.session_id)
                    if any(receipt.get(key) != getattr(row, key) for key in ("digest", "run_id", "operation", "owner")):
                        continue
                    result = receipt["result"]
                    payload = json.loads(result)
                    status = payload.get("status") or ("succeeded" if payload.get("ok") else "failed")
                    if status not in ("succeeded", "failed", "denied", "cancelled") or payload.get("execution_id") != row.id:
                        continue
                    changed = await db.execute(update(ExecutionRecord).where(
                        ExecutionRecord.id == row.id, ExecutionRecord.status == row.status, ExecutionRecord.updated_at == row.updated_at,
                    ).values(status=status, result=result, updated_at=datetime.now(timezone.utc).isoformat()))
                    count += changed.rowcount
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            await db.commit()
        return {"reconciled": count, "evidence": "durable_receipt", "unknown_without_receipt": "not_replayed"}

    async def _change(self, execution_id, status, result, allowed, backend=None):
        async with self._sessions() as db:
            values = {"status": status, "result": result, "updated_at": datetime.now(timezone.utc).isoformat()}
            if backend is not None:
                values["backend"] = backend
            changed = await db.execute(update(ExecutionRecord).where(
                ExecutionRecord.id == execution_id, ExecutionRecord.owner == self.owner,
                ExecutionRecord.status.in_(allowed),
            ).values(**values))
            if changed.rowcount != 1:
                raise ValueError("execution ownership or state changed")
            await db.commit()

    def _view(self, row, include_result):
        status = row.status
        if status in ("prepared", "running") and (row.owner != self.owner or row.id not in self.active):
            status = "result_unknown"
        data = {"execution_id": row.id, "session_id": row.session_id, "run_id": row.run_id,
                "operation": row.operation, "location": row.location, "backend": row.backend,
                "status": status, "created_at": row.created_at, "updated_at": row.updated_at}
        if include_result:
            data["result"] = row.result
        return data

    async def read(self, execution_id, *, include_result=False):
        async with self._sessions() as db:
            row = await db.get(ExecutionRecord, execution_id)
            return self._view(row, include_result) if row else None

    async def recent(self, session_id=None, limit=50):
        query = select(ExecutionRecord).order_by(ExecutionRecord.created_at.desc()).limit(min(max(limit, 1), 100))
        if session_id is not None:
            query = query.where(ExecutionRecord.session_id == session_id)
        async with self._sessions() as db:
            return [self._view(row, False) for row in (await db.execute(query)).scalars()]
