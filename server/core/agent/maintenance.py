"""Archive confirmed terminal runtime evidence before deleting database history."""
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update

from server.core.agent.store import RunCheckpointModel, RunEventModel
from server.core.execution.store import ExecutionRecord
from server.core.execution.approvals import ApprovalRecord


def row_data(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class RuntimeArchive:
    def __init__(self, sessions, directory, max_bytes, outputs=None, receipts=None):
        self.sessions, self.directory, self.max_bytes = sessions, Path(directory), max_bytes
        self.outputs, self.receipts = outputs, receipts

    def write(self, run_id, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(run_id.encode()).hexdigest() + ".json.gz"
        path = self.directory / name
        encoded = gzip.compress(json.dumps(data, ensure_ascii=False, sort_keys=True).encode(), mtime=0)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != encoded:
                raise ValueError("runtime archive identity or integrity mismatch")
            return name
        used = sum(p.stat().st_size for p in self.directory.glob("*.json.gz"))
        if used + len(encoded) > self.max_bytes:
            raise ValueError("runtime archive storage budget exceeded; evidence retained in database")
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return name

    async def prune(self, run_ids):
        count = 0
        for run_id in sorted(run_ids):
            async with self.sessions() as db:
                checkpoint = await db.get(RunCheckpointModel, run_id)
                if checkpoint is None:
                    continue
                data = json.loads(checkpoint.snapshot)
                run = data["run"]
                if (run.get("outcome") or {}).get("status") not in ("completed", "failed", "cancelled") or any(t["status"] != "returned" for t in run["tool_executions"]):
                    continue
                executions = list((await db.execute(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))).scalars())
                if any(row.status not in ("succeeded", "failed", "denied", "cancelled") for row in executions):
                    continue
                ids = [row.id for row in executions]
                approvals = list((await db.execute(select(ApprovalRecord).where(ApprovalRecord.execution_id.in_(ids)))).scalars()) if ids else []
                if any(row.status in ("pending", "approved") for row in approvals):
                    continue
                events = list((await db.execute(select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.revision))).scalars())
                bundle = {"schema_version": 1, "checkpoint": row_data(checkpoint), "events": [row_data(row) for row in events],
                          "executions": [row_data(row) for row in executions], "approvals": [row_data(row) for row in approvals]}
                for name, store in (("outputs", self.outputs), ("receipts", self.receipts)):
                    bundle[name] = {}
                    if store is not None:
                        for execution in executions:
                            if store._path(execution.id).exists():
                                bundle[name][execution.id] = await asyncio.to_thread(store.receipt, execution.id, execution.session_id)
                await asyncio.to_thread(self.write, run_id, bundle)
                removed = await db.execute(delete(RunCheckpointModel).where(RunCheckpointModel.id == run_id, RunCheckpointModel.revision == checkpoint.revision))
                if removed.rowcount != 1:
                    raise ValueError("runtime changed during archival")
                await db.execute(delete(RunEventModel).where(RunEventModel.run_id == run_id))
                if ids:
                    await db.execute(delete(ApprovalRecord).where(ApprovalRecord.execution_id.in_(ids)))
                    # Retain identity tombstones so an old execution ID never becomes runnable again.
                    await db.execute(update(ExecutionRecord).where(ExecutionRecord.id.in_(ids)).values(status="archived", result=None))
                await db.commit()
                for store in (self.outputs, self.receipts):
                    if store is not None:
                        for identifier in ids:
                            store._path(identifier).unlink(missing_ok=True)
                count += 1
        return count
