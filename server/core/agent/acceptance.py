"""Explicit, user-supplied acceptance checks backed by local evidence."""
import asyncio
import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.core.agent.policy import StopAction, StopDecision


class AcceptanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["file", "command"]
    path: str = Field(default="", max_length=2000)
    contains: str | None = Field(default=None, max_length=4096)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    json_valid: bool = False
    operation: Literal["run_shell", "run_python"] = "run_shell"
    source: str = Field(default="", max_length=16000)

    @model_validator(mode="after")
    def bounded_scope(self):
        if self.kind == "file":
            path = PurePosixPath(self.path.replace("\\", "/"))
            if not self.path or path.is_absolute() or ".." in path.parts or ":" in self.path:
                raise ValueError("acceptance file must be a relative workspace path")
            from server.core.execution.snapshots import SnapshotService
            if any(SnapshotService._excluded(part) for part in path.parts):
                raise ValueError("acceptance file is protected or excluded")
        elif not self.source.strip():
            raise ValueError("command acceptance requires an exact command or Python source")
        return self


class AcceptancePolicy:
    async def on_stop(self, ctx, answer):
        run = ctx.agent_run
        if not run.acceptance:
            run.verification = {"status": "not_requested", "checks": []}
            return StopDecision()
        settings, snapshots = ctx.extras.get("settings"), ctx.extras.get("snapshots")
        checks = []
        for raw in run.acceptance:
            criterion = AcceptanceCheck.model_validate(raw)
            passed = False
            if criterion.kind == "file" and settings is not None and snapshots is not None:
                from server.core.computer.workspace import workspace_root
                try:
                    root = workspace_root(settings.workspaces_dir, ctx.session_id)
                    data, _ = await asyncio.to_thread(snapshots._read, snapshots._root(root), criterion.path.replace("\\", "/"))
                    passed = (criterion.sha256 is None or hashlib.sha256(data).hexdigest() == criterion.sha256)
                    if criterion.contains is not None:
                        passed &= criterion.contains in data.decode("utf-8")
                    if criterion.json_valid:
                        json.loads(data)
                except (OSError, ValueError, UnicodeError, TypeError):
                    passed = False
            elif criterion.kind == "command":
                expected = hashlib.sha256(criterion.source.encode()).hexdigest()
                matching = [i for i, tool in enumerate(run.tool_executions) if tool.get("name") == criterion.operation
                            and tool.get("source_digest") == expected and tool.get("status") == "returned"]
                if matching:
                    index = matching[-1]
                    try:
                        result = json.loads(run.tool_executions[index]["result"])
                        later_writes = any(t.get("name") in ("run_shell", "run_python", "write_file")
                                           and t.get("workspace_changed") is not False for t in run.tool_executions[index + 1:])
                        passed = result.get("ok") is True and result.get("exit_code") == 0 and not later_writes
                    except (ValueError, KeyError, TypeError):
                        pass
            checks.append({"kind": criterion.kind, "path": criterion.path if criterion.kind == "file" else None, "passed": bool(passed)})
        run.verification = {"status": "passed" if all(c["passed"] for c in checks) else "failed", "checks": checks}
        await ctx.publish("acceptance_checked", run.verification)
        if run.verification["status"] == "passed":
            return StopDecision()
        return StopDecision(StopAction.CONTINUE, "Acceptance checks are not satisfied. Complete and verify the original criteria: " + json.dumps(run.acceptance, ensure_ascii=False))
