"""Run shell commands inside session workspace."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from server.core.computer.policy import check_shell_command
from server.core.computer.workspace import workspace_root


async def run_shell(
    command: str,
    *,
    workspaces_dir: str,
    session_id: str,
    shell: str = "powershell",
    timeout: int = 60,
) -> str:
    denied = check_shell_command(command)
    if denied:
        return json.dumps({"ok": False, "error": denied}, ensure_ascii=False)
    root = workspace_root(workspaces_dir, session_id)
    timeout = max(5, min(int(timeout), 300))
    if shell == "cmd" and sys.platform == "win32":
        args = ["cmd.exe", "/c", command]
    elif shell == "bash":
        args = ["bash", "-lc", command]
    elif sys.platform == "win32":
        args = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    else:
        args = ["bash", "-lc", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return json.dumps(
                {"ok": False, "error": f"timeout after {timeout}s"},
                ensure_ascii=False,
            )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    out = _clip(stdout_b.decode("utf-8", errors="replace"))
    err = _clip(stderr_b.decode("utf-8", errors="replace"))
    payload: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "cwd": str(root),
        "stdout": out,
        "stderr": err,
    }
    return json.dumps(payload, ensure_ascii=False)


def _clip(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
