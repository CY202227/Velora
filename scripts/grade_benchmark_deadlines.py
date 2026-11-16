"""Inspect stopped deadline artifacts without changing their official benchmark outcome."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from server.config import Settings
from server.core.conversation.store import ConversationStore
from server.core.execution.service import execute
from server.core.execution.snapshots import SnapshotService
from server.core.execution.store import ExecutionStore


async def main(report_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    root = Path(report["root"])
    destination = root / "deadline-diagnostics"
    destination.mkdir(exist_ok=True)
    output = destination / "results.json"
    results = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    rows = {row["task_id"]: row for row in map(json.loads, Path("refs/octopack/evaluation/create/humaneval-x/data/python/data/humanevalpack.jsonl").read_text(encoding="utf-8").splitlines())}
    settings = Settings(_env_file=None, computer_enabled=True, execution_location="local", execution_backend="native",
                        workspaces_dir=str(destination / "workspaces"), execution_policy_dir=str(destination / "policies"), computer_timeout_seconds=30)
    db = ConversationStore("sqlite+aiosqlite:///" + (destination / "runtime.db").as_posix())
    await db.init()
    store = ExecutionStore(db._session_factory)
    snapshots = SnapshotService(destination / "checkpoints")
    try:
        for result in report["results"]:
            task_id = result["task_id"]
            if result.get("error_type") != "TimeoutError" or not result.get("run_id") or task_id in results:
                continue
            with sqlite3.connect("file:" + (root / "runtime.db").as_posix() + "?mode=ro", uri=True) as source:
                saved = source.execute("select snapshot from agent_run_checkpoints where id=?", (result["run_id"],)).fetchone()
                if not saved:
                    continue
                checkpoint = json.loads(saved[0])
                pending = source.execute("select count(*) from execution_records where run_id=? and status in ('prepared','running','result_unknown')", (result["run_id"],)).fetchone()[0]
            if pending or (checkpoint["run"].get("outcome") or {}).get("status") != "cancelled":
                continue
            workspace = root / "workspaces" / checkpoint["session_id"]
            contents = snapshots._read(snapshots._root(workspace), "solution.py")
            if contents is None:
                continue
            code = contents[0].decode("utf-8-sig") + "\n\n" + rows[task_id]["test"] + "\nprint('POSTHOC_TESTS_PASSED')\n"
            graded = json.loads(await execute(settings, "posthoc-" + uuid4().hex, "run_python", {"code": code}, execution_id=uuid4().hex, store=store))
            results[task_id] = {"artifact_passed_after_deadline": graded.get("ok") is True and "POSTHOC_TESTS_PASSED" in graded.get("stdout", ""),
                                "reason": graded.get("reason"), "stderr": graded.get("stderr", "")[-2000:],
                                "original_score_unchanged": True, "agent_received_hidden_test_feedback": False}
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"task_id": task_id, **results[task_id]}, ensure_ascii=False))
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    asyncio.run(main(parser.parse_args().report))
