"""Durable model-stage checkpoints and append-only boundary records."""

from __future__ import annotations

import json
from dataclasses import asdict
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

from server.core.conversation.store import Base


class RunCheckpointModel(Base):
    __tablename__ = "agent_run_checkpoints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[str] = mapped_column(Text)


class RunEventModel(Base):
    __tablename__ = "agent_run_events"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    boundary: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class RunStore:
    def __init__(self, session_factory, admission=None, max_checkpoint_bytes=8 * 1024 * 1024):
        self._sessions = session_factory
        self.admission, self.max_checkpoint_bytes = admission, max_checkpoint_bytes

    async def checkpoint(self, ctx, boundary: str) -> None:
        run = ctx.agent_run
        if run is None:
            return
        if self.admission is not None and run.outcome is None:
            await self.admission()
        data = {
            "schema_version": 1, "session_id": ctx.session_id,
            "model": ctx.model, "user_text": ctx.request.user_text,
            "assistant_text": ctx.assistant_text, "stop_reason": ctx.stop_reason,
            "run": asdict(run),
            "resume_next_step": boundary == "output_continuation_ready",
        }
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode()) > self.max_checkpoint_bytes:
            from server.core.agent.storage_budget import StorageBudgetExceeded
            raise StorageBudgetExceeded("单次运行检查点超出存储预算，已停止以免丢失恢复证据。")
        # Provider credentials belong to connection configuration, never checkpoints.
        if ctx.llm_api_key and ctx.llm_api_key in encoded:
            data["credentials_redacted"] = True
            encoded = json.dumps(data, ensure_ascii=False).replace(ctx.llm_api_key, "[redacted]")
        async with self._sessions() as db:
            row = await db.get(RunCheckpointModel, run.run_id)
            if row is not None and json.loads(row.snapshot).get("claimed_by"):
                raise ValueError("Run ownership transferred to a recovery successor")
            revision = row.revision + 1 if row else 1
            if row is None:
                db.add(RunCheckpointModel(id=run.run_id, session_id=ctx.session_id, revision=revision, snapshot=encoded))
            else:
                previous_revision = row.revision
                changed = await db.execute(update(RunCheckpointModel).where(
                    RunCheckpointModel.id == run.run_id,
                    RunCheckpointModel.revision == previous_revision,
                ).values(revision=revision, snapshot=encoded))
                if changed.rowcount != 1:
                    raise ValueError("Checkpoint changed concurrently")
            db.add(RunEventModel(run_id=run.run_id, revision=revision, boundary=boundary,
                                 payload=json.dumps({"step": run.step, "phase": run.phase, "outcome": asdict(run.outcome) if run.outcome else None})))
            await db.commit()

    async def read(self, run_id: str) -> dict | None:
        async with self._sessions() as db:
            row = await db.get(RunCheckpointModel, run_id)
            if row is None:
                return None
            data = json.loads(row.snapshot)
            data["revision"] = row.revision
            # A persisted in-flight invocation cannot be assumed to have rolled back.
            for tool in data["run"]["tool_executions"]:
                if tool["status"] == "running":
                    tool["status"] = "result_unknown"
            data["replay_safe"] = (
                not data.get("claimed_by") and not data.get("credentials_redacted")
                and
                data["run"].get("outcome") is None
                and data["run"]["phase"] == "requesting"
                and all(t["status"] == "returned" for t in data["run"]["tool_executions"])
            )
            return data

    async def claim(self, run_id: str, feedback: str = "") -> dict:
        """Atomically claim a source and create its successor before execution."""
        if self.admission is not None:
            await self.admission()
        data = await self.read(run_id)
        if data is None:
            raise ValueError("Run checkpoint not found")
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported checkpoint schema")
        waiting = (data["run"].get("outcome") or {}).get("status") == "waiting"
        reconciled = False
        if not data["replay_safe"] and not waiting and not data.get("claimed_by") and not data.get("credentials_redacted"):
            reconciled = await self._close_saved_tool_batch(data, allow_pending=True)
        if data.get("claimed_by") or data.get("credentials_redacted") or not (data["replay_safe"] or waiting):
            raise ValueError("Run is terminal, already claimed, or not at a safe boundary")
        if waiting and (not feedback.strip() or any(t["status"] != "returned" for t in data["run"]["tool_executions"])):
            raise ValueError("Waiting run needs input and fully resolved tools")
        abandoned_prepared = data.pop("abandoned_prepared", [])
        child = deepcopy(data)
        child_id = uuid4().hex
        child["parent_run_id"] = run_id
        child["run"]["run_id"] = child_id
        child["run"]["outcome"] = None
        child["run"]["phase"] = "executing_tools" if data.get("resume_tool_batch") else "requesting"
        child["stop_reason"] = None
        child["assistant_text"] = ""
        if waiting:
            child["run"]["messages"].append({"role": "user", "content": feedback.strip()})
        child["resume_next_step"] = waiting or reconciled or bool(data.get("resume_next_step"))
        revision = data.pop("revision")
        data["claimed_by"] = child_id
        from server.core.execution.store import ExecutionRecord
        from server.core.execution.approvals import ApprovalRecord, now
        async with self._sessions() as db:
            for abandoned in abandoned_prepared:
                changed = await db.execute(update(ExecutionRecord).where(
                    ExecutionRecord.id == abandoned["id"], ExecutionRecord.status == "prepared",
                ).values(status="denied", result=abandoned["result"], updated_at=now()))
                if changed.rowcount != 1:
                    raise ValueError("Prepared execution changed during recovery")
                await db.execute(update(ApprovalRecord).where(
                    ApprovalRecord.execution_id == abandoned["id"],
                    ApprovalRecord.status.in_(("pending", "approved")),
                ).values(status="cancelled"))
            result = await db.execute(update(RunCheckpointModel).where(
                RunCheckpointModel.id == run_id, RunCheckpointModel.revision == revision,
            ).values(revision=revision + 1, snapshot=json.dumps(data, ensure_ascii=False)))
            if result.rowcount != 1:
                raise ValueError("Checkpoint changed or was claimed concurrently")
            db.add(RunEventModel(run_id=run_id, revision=revision + 1, boundary="resume_claimed", payload=json.dumps({"successor": child_id})))
            db.add(RunCheckpointModel(id=child_id, session_id=child["session_id"], revision=1, snapshot=json.dumps(child, ensure_ascii=False)))
            db.add(RunEventModel(run_id=child_id, revision=1, boundary="resume_created", payload=json.dumps({"parent": run_id})))
            await db.commit()
        return child

    async def _close_saved_tool_batch(self, data, *, allow_pending=False) -> bool:
        """Reattach proven results only. Never dispatch a tool to discover its outcome."""
        run = data["run"]
        if run.get("outcome") is not None or run["phase"] not in ("executing_tools", "awaiting_approval"):
            return False
        messages = run["messages"]
        indices = [i for i, message in enumerate(messages) if message.get("role") == "assistant" and message.get("tool_calls")]
        if not indices:
            return False
        index = indices[-1]
        calls = messages[index]["tool_calls"]
        ids = [call.get("id") for call in calls]
        if not ids or None in ids or len(set(ids)) != len(ids):
            return False
        tail = messages[index + 1:]
        if any(message.get("role") != "tool" for message in tail) or [message.get("tool_call_id") for message in tail] != ids[:len(tail)]:
            return False
        records = run["tool_executions"]
        batch = [record for record in records if record.get("round") == run["step"] - 1]
        if len(batch) != len(calls) or any(record.get("status") != "returned" for record in records if record not in batch):
            return False
        from server.core.execution.store import ExecutionRecord
        results = []
        pending = False
        async with self._sessions() as db:
            for position, (call, record) in enumerate(zip(calls, batch)):
                if record.get("id") != call["id"] or record.get("name") != call.get("function", {}).get("name"):
                    return False
                if record.get("status") == "not_started" and allow_pending:
                    if position < len(tail) or record.get("execution_id"):
                        return False
                    from uuid import NAMESPACE_URL, uuid5
                    expected = uuid5(NAMESPACE_URL, f"velora:{run['run_id']}:{run['step'] - 1}:{position}:{call['id']}").hex
                    if await db.get(ExecutionRecord, expected) is not None:
                        return False
                    pending = True
                    results.append(None)
                    continue
                if pending:
                    return False
                result = record.get("result") if record.get("status") == "returned" else None
                if result is None:
                    if record.get("status") not in ("running", "result_unknown") or not record.get("execution_id"):
                        return False
                    from uuid import NAMESPACE_URL, uuid5
                    expected = uuid5(NAMESPACE_URL, f"velora:{run['run_id']}:{run['step'] - 1}:{position}:{call['id']}").hex
                    if record["execution_id"] != expected:
                        return False
                    saved = await db.get(ExecutionRecord, expected)
                    if (saved is None or saved.run_id != run["run_id"] or saved.session_id != data["session_id"]
                            or saved.operation != record["name"]):
                        return False
                    if allow_pending and saved.status == "prepared":
                        result = json.dumps({"execution_id": expected, "ok": False, "status": "denied",
                                             "reason": "approval_interrupted", "error": "Previous preparation was abandoned; no workload started. Request fresh authorization if still needed."})
                        data.setdefault("abandoned_prepared", []).append({"id": expected, "result": result})
                    elif saved.status in ("succeeded", "failed", "denied", "cancelled") and saved.result is not None:
                        result = saved.result
                    else:
                        return False
                if not isinstance(result, str):
                    return False
                try:
                    payload = json.loads(result)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and (payload.get("status") == "result_unknown" or payload.get("reason") == "execution_not_replayed"):
                    return False
                if (record.get("execution_id") and isinstance(payload, dict)
                        and payload.get("execution_id", record["execution_id"]) != record["execution_id"]):
                    return False
                if position < len(tail) and tail[position].get("content") != result:
                    return False
                results.append(result)
        for call, record, result in zip(calls, batch, results):
            if result is not None:
                record.update(status="returned", result=result)
        messages[index + 1:] = [{"role": "tool", "tool_call_id": call["id"], "content": result} for call, result in zip(calls, results) if result is not None]
        run["phase"] = "executing_tools" if pending else "requesting"
        data["replay_safe"] = True
        data["resume_tool_batch"] = pending
        data["recovery_kind"] = "pending_tool_batch" if pending else "closed_tool_batch"
        return True

    async def recovery_status(self, run_id):
        data = await self.read(run_id)
        if data is None:
            return None
        if data.get("schema_version") != 1 or data.get("claimed_by") or data.get("credentials_redacted"):
            return {"recoverable": False, "kind": None}
        waiting = (data["run"].get("outcome") or {}).get("status") == "waiting"
        if waiting and all(tool["status"] == "returned" for tool in data["run"]["tool_executions"]):
            return {"recoverable": True, "kind": "waiting_for_input"}
        if data["replay_safe"]:
            return {"recoverable": True, "kind": "request_ready"}
        closed = await self._close_saved_tool_batch(data, allow_pending=True)
        return {"recoverable": closed, "kind": data.get("recovery_kind") if closed else None}

    async def recent(self, limit: int = 50) -> list[dict]:
        async with self._sessions() as db:
            rows = (await db.execute(select(RunCheckpointModel).limit(min(max(limit, 1), 100)))).scalars()
            return [{"run_id": row.id, "session_id": row.session_id, "revision": row.revision,
                     "phase": json.loads(row.snapshot)["run"]["phase"],
                     "outcome": json.loads(row.snapshot)["run"]["outcome"]} for row in rows]
