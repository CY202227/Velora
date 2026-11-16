"""Own live chat tasks independently of transport; persist bounded-page event replay."""
from __future__ import annotations

import asyncio
import json
import logging
import anyio
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from sqlalchemy import Integer, String, Text, select, update, delete
from sqlalchemy.orm import Mapped, mapped_column

from server.core.chain.types import StreamEvent
from server.core.conversation.store import Base


class TaskRecord(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    owner: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    cursor: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40))
    result: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskEvent(Base):
    __tablename__ = "agent_task_events"
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    data: Mapped[str] = mapped_column(Text)


class TaskService:
    def __init__(self, sessions, router, settings, admission=None):
        self._sessions, self.router, self.settings = sessions, router, settings
        self.owner = uuid4().hex
        self.active = {}
        self._task_sessions = {}
        self.busy = set()
        self._event_bytes = {}
        self.admission = admission

    async def submit(self, request, *, resume_run_id=None, runner=None):
        if self.admission is not None:
            await self.admission()
        if request.session_id in self.busy:
            raise ValueError("a task is already active in this session; reconnect or cancel it")
        self.busy.add(request.session_id)
        identifier = uuid4().hex
        self._event_bytes[identifier] = 0
        try:
            async with self._sessions() as db:
                db.add(TaskRecord(id=identifier, session_id=request.session_id, owner=self.owner,
                                  status="running", cursor=0,
                                  created_at=datetime.now(timezone.utc).isoformat()))
                await db.commit()
            await self._append(identifier, StreamEvent("task_started", {"session_id": request.session_id, "resume_of": resume_run_id}))
            started = asyncio.Event()
            task = asyncio.create_task(self._run(identifier, request, started, resume_run_id, runner))
            self.active[identifier] = task
            self._task_sessions[identifier] = request.session_id
            await started.wait()
            return identifier
        except BaseException:
            if identifier not in self.active:
                self.busy.discard(request.session_id)
                self._event_bytes.pop(identifier, None)
            raise

    async def _append(self, identifier, event):
        async with self._sessions() as db:
            row = await db.get(TaskRecord, identifier)
            if row is None or row.owner != self.owner or row.status != "running":
                raise ValueError("task ownership or terminal state changed")
            cursor = row.cursor + 1
            payload = {**event.data, "task_id": identifier, "cursor": cursor}
            if event.type == "turn_end":
                payload["finished_at"] = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(payload, ensure_ascii=False)
            secret = self.settings.llm_api_key
            if secret:
                # Redact values without corrupting JSON syntax.
                def scrub(value):
                    if isinstance(value, str):
                        return value.replace(secret, "[redacted]")
                    if isinstance(value, dict):
                        return {k: scrub(v) for k, v in value.items()}
                    if isinstance(value, list):
                        return [scrub(v) for v in value]
                    return value
                encoded = json.dumps(scrub(payload), ensure_ascii=False)
            values = {"cursor": cursor}
            size = len(encoded.encode("utf-8"))
            if event.type != "turn_end" and self._event_bytes.get(identifier, 0) + size > self.settings.task_event_budget_bytes:
                raise EventStorageBudget("task event storage budget exhausted")
            if event.type == "turn_end":
                status = (payload.get("result") or {}).get("status", "failed")
                values.update(status=status if status in ("completed", "failed", "cancelled", "waiting") else "failed", result=encoded)
            changed = await db.execute(update(TaskRecord).where(
                TaskRecord.id == identifier, TaskRecord.owner == self.owner,
                TaskRecord.status == "running", TaskRecord.cursor == row.cursor,
            ).values(**values))
            if changed.rowcount != 1:
                raise ValueError("task changed concurrently")
            db.add(TaskEvent(task_id=identifier, cursor=cursor, kind=event.type, data=encoded))
            await db.commit()
            self._event_bytes[identifier] = self._event_bytes.get(identifier, 0) + size

    async def _run(self, identifier, request, started, resume_run_id=None, runner=None):
        started.set()
        async def emit(event):
            await self._append(identifier, event)
        try:
            options = {"resume_run_id": resume_run_id} if resume_run_id is not None else {}
            if runner is None:
                await self.router.handle(request, emit=emit, **options)
            else:
                await runner(emit)
            snapshot = await self.read(identifier)
            if snapshot["status"] == "running":
                await emit(StreamEvent("turn_end", {"result": {"status": "failed", "reason": "terminal_missing", "message": "任务未返回结束确认，请检查运行记录。"}}))
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            quota = isinstance(exc, EventStorageBudget)
            if not cancelled:
                logging.getLogger(__name__).exception("Detached task failed")
            try:
                snapshot = await self.read(identifier)
                if snapshot and snapshot["status"] == "running":
                    await emit(StreamEvent("turn_end", {"result": {
                        "status": "cancelled" if cancelled else "failed",
                        "reason": "cancelled" if cancelled else "event_storage_budget_exhausted" if quota else "task_failed",
                        "message": "任务已取消，已发生的外部操作不会自动撤销。" if cancelled else "任务事件存储预算耗尽，已停止，请核对执行记录。" if quota else "任务执行异常，请核对检查点与执行记录，不要盲目重发。",
                    }}))
            except Exception:
                logging.getLogger(__name__).exception("Task terminal record could not be saved")
        finally:
            self.active.pop(identifier, None)
            self._task_sessions.pop(identifier, None)
            self.busy.discard(request.session_id)
            self._event_bytes.pop(identifier, None)

    async def read(self, identifier):
        async with self._sessions() as db:
            row = await db.get(TaskRecord, identifier)
            if row is None:
                return None
            status = row.status
            if status == "running" and (row.owner != self.owner or identifier not in self.active):
                status = "interrupted"
            return {"task_id": row.id, "session_id": row.session_id, "status": status,
                    "cursor": row.cursor, "created_at": row.created_at,
                    "result": json.loads(row.result) if row.result else None}

    async def watch(self, identifier, after=0):
        while True:
            # Finish returning the read connection before an SSE cancel scope exits.
            with anyio.CancelScope(shield=True):
                snapshot = await self.read(identifier)
                if snapshot is None:
                    raise ValueError("task not found")
                async with self._sessions() as db:
                    rows = list((await db.execute(select(TaskEvent).where(
                        TaskEvent.task_id == identifier, TaskEvent.cursor > after,
                    ).order_by(TaskEvent.cursor).limit(100))).scalars())
            for row in rows:
                after = row.cursor
                yield {"event": row.kind, "id": f"{identifier}:{after}", "data": row.data}
            if rows:
                continue
            if snapshot["status"] != "running":
                if snapshot["status"] == "interrupted":
                    yield {"event": "task_interrupted", "data": json.dumps({
                        "task_id": identifier, "message": "任务执行进程已不可确认，请核对检查点；不会自动重放。",
                    }, ensure_ascii=False)}
                return
            await asyncio.sleep(.2)

    async def recent(self, session_id):
        async with self._sessions() as db:
            identifiers = list((await db.execute(select(TaskRecord.id).where(
                TaskRecord.session_id == session_id,
            ).order_by(TaskRecord.created_at.desc()).limit(20))).scalars())
        snapshots = [await self.read(identifier) for identifier in identifiers]
        return [{k: v for k, v in row.items() if k != "result"} for row in snapshots]

    def cancel(self, identifier):
        task = self.active.get(identifier)
        if task is None or task.done() or task.cancelling():
            return False
        cancel = getattr(self.router, "cancel", None)
        if cancel and cancel(self._task_sessions[identifier], owner_task=task):
            return True
        task.cancel()
        return True

    async def close(self):
        tasks = list(self.active.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def prune(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.task_history_days)).isoformat()
        async with self._sessions() as db:
            candidates = list((await db.execute(select(TaskRecord).where(
                TaskRecord.created_at < cutoff, TaskRecord.status.in_(("completed", "failed", "cancelled")),
            ).limit(500))).scalars())
            identifiers = [row.id for row in candidates if (json.loads(row.result or "{}").get("finished_at") or row.created_at) < cutoff]
            if identifiers:
                await db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(identifiers)))
                await db.execute(delete(TaskRecord).where(TaskRecord.id.in_(identifiers)))
                await db.commit()
            return len(identifiers)

    async def expired_run_ids(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.task_history_days)).isoformat()
        async with self._sessions() as db:
            records = list((await db.execute(select(TaskRecord).where(
                TaskRecord.created_at < cutoff, TaskRecord.status.in_(("completed", "failed", "cancelled")),
            ).limit(500))).scalars())
            identifiers = set()
            for record in records:
                data = json.loads(record.result or "{}")
                if (data.get("finished_at") or record.created_at) < cutoff and data.get("result", {}).get("run_id"):
                    identifiers.add(data["result"]["run_id"])
            return identifiers


class EventStorageBudget(RuntimeError):
    pass
