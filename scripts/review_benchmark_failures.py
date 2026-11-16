"""Offline counterfactual probes of benchmark workflow friction; no model requests."""
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
    destination = root / "workflow-probes"
    destination.mkdir(exist_ok=True)
    settings = Settings(_env_file=None, computer_enabled=True, workspaces_dir=str(destination / "workspaces"),
                        execution_policy_dir=str(destination / "policies"), computer_timeout_seconds=15)
    db = ConversationStore("sqlite+aiosqlite:///" + (destination / "runtime.db").as_posix())
    await db.init()
    store = ExecutionStore(db._session_factory)
    snapshots = SnapshotService(destination / "checkpoints")
    identifier = "probe-" + uuid4().hex
    workspace = Path(settings.workspaces_dir) / identifier
    workspace.mkdir(parents=True)
    row = next(r for r in report["results"] if r["task_id"] == "Python/93")
    with sqlite3.connect("file:" + (root / "runtime.db").as_posix() + "?mode=ro", uri=True) as source:
        checkpoint = json.loads(source.execute("select snapshot from agent_run_checkpoints where id=?", (row["run_id"],)).fetchone()[0])
    original = root / "workspaces" / checkpoint["session_id"]
    contents = snapshots._read(snapshots._root(original), "solution.py")
    (workspace / "solution.py").write_bytes(contents[0])
    async def run(operation, arguments):
        return json.loads(await execute(settings, identifier, operation, arguments, execution_id=uuid4().hex, store=store))
    try:
        baseline = await run("run_python", {"code": "from solution import encode; print(encode('AEIOU'))"})
        accessible = await run("run_python", {"code": "import sys,os; sys.path.append(os.getcwd()); from solution import encode; print(encode('AEIOU'))"})
        version = await run("run_shell", {"command": "$PSVersionTable.PSVersion.ToString()"})
        incompatible = await run("run_shell", {"command": "python -c \"print('first')\" && python -c \"print('second')\""})
        compatible = await run("run_shell", {"command": "python -c \"print('first')\"; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -c \"print('second')\"; exit $LASTEXITCODE"})
        evidence = {"model_called": False, "original_workspaces_modified": False,
            "workspace_import": {"default_failed": baseline.get("ok") is False, "error": baseline.get("stderr", ""),
                "explicit_workspace_path_succeeded": accessible.get("ok") is True,
                "same_wrong_implementation_output": accessible.get("stdout", "").strip(), "expected_for_uppercase_vowels": "cgkqw"},
            "shell": {"version": version.get("stdout", "").strip(), "and_and_failed": incompatible.get("ok") is False,
                "compatible_sequence_succeeded": compatible.get("ok") is True, "output": compatible.get("stdout", "").strip()}}
        (destination / "results.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    asyncio.run(main(parser.parse_args().report))
