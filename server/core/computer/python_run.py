"""Run Python snippets in session workspace."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from server.core.computer.workspace import workspace_root


async def run_python(
    code: str,
    *,
    workspaces_dir: str,
    session_id: str,
    timeout: int = 60,
) -> str:
    text = (code or "").strip()
    if not text:
        return json.dumps({"ok": False, "error": "empty code"}, ensure_ascii=False)
    root = workspace_root(workspaces_dir, session_id)
    script = root / f"_velora_run_{uuid.uuid4().hex[:10]}.py"
    timeout = max(5, min(int(timeout), 300))
    try:
        script.write_text(text, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
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
    finally:
        try:
            script.unlink(missing_ok=True)
        except Exception:
            pass

    def clip(s: str) -> str:
        if len(s) <= 12000:
            return s
        return s[:12000] + f"\n...[truncated {len(s) - 12000} chars]"

    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "cwd": str(root),
            "stdout": clip(stdout_b.decode("utf-8", errors="replace")),
            "stderr": clip(stderr_b.decode("utf-8", errors="replace")),
        },
        ensure_ascii=False,
    )
