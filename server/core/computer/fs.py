"""File operations inside session workspace."""

from __future__ import annotations

import json
from pathlib import Path

from server.core.computer.workspace import WorkspaceError, resolve_in_workspace


def read_file(
    path: str,
    *,
    workspaces_dir: str,
    session_id: str,
    allow_abs: bool = False,
    extra_roots: list[str] | None = None,
    max_chars: int = 100_000,
) -> str:
    try:
        target = resolve_in_workspace(
            workspaces_dir,
            session_id,
            path,
            allow_abs=allow_abs,
            extra_roots=extra_roots,
        )
        if not target.is_file():
            return json.dumps(
                {"ok": False, "error": f"not a file: {path}"}, ensure_ascii=False
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "content": text,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )
    except WorkspaceError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def write_file(
    path: str,
    content: str,
    *,
    workspaces_dir: str,
    session_id: str,
    allow_abs: bool = False,
) -> str:
    try:
        # writes stay in workspace only (no skills_dir writes)
        target = resolve_in_workspace(
            workspaces_dir, session_id, path, allow_abs=allow_abs
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if content is not None else "", encoding="utf-8")
        return json.dumps(
            {"ok": True, "path": str(target), "bytes": target.stat().st_size},
            ensure_ascii=False,
        )
    except WorkspaceError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def list_dir(
    path: str | None = ".",
    *,
    workspaces_dir: str,
    session_id: str,
    allow_abs: bool = False,
) -> str:
    try:
        target = resolve_in_workspace(
            workspaces_dir, session_id, path or ".", allow_abs=allow_abs
        )
        if not target.is_dir():
            return json.dumps(
                {"ok": False, "error": f"not a directory: {path}"},
                ensure_ascii=False,
            )
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower())[:200]:
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return json.dumps(
            {"ok": True, "path": str(target), "entries": entries},
            ensure_ascii=False,
        )
    except WorkspaceError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
