"""Workspace file snapshot / attachment diff for turn cards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.core.computer.workspace import workspace_root

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAX_ATTACHMENTS = 8
_MAX_BYTES = 8 * 1024 * 1024


def snapshot_workspace(workspaces_dir: str, session_id: str) -> dict[str, float]:
    """Map relative posix path -> mtime for files under the session workspace."""
    root = workspace_root(workspaces_dir, session_id)
    out: dict[str, float] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _skip_rel(rel):
            continue
        try:
            out[rel] = path.stat().st_mtime
        except OSError:
            continue
    return out


def diff_attachments(
    workspaces_dir: str,
    session_id: str,
    before: dict[str, float] | None,
) -> list[dict[str, Any]]:
    """Return new/changed files since `before` snapshot as attachment dicts."""
    root = workspace_root(workspaces_dir, session_id)
    prev = before or {}
    hits: list[tuple[float, dict[str, Any]]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _skip_rel(rel):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        old = prev.get(rel)
        if old is not None and abs(st.st_mtime - old) < 1e-6:
            continue
        if st.st_size > _MAX_BYTES:
            continue
        kind = "image" if path.suffix.lower() in _IMAGE_EXTS else "file"
        hits.append(
            (
                st.st_mtime,
                {
                    "path": rel,
                    "name": path.name,
                    "bytes": int(st.st_size),
                    "kind": kind,
                },
            )
        )
    hits.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in hits[:_MAX_ATTACHMENTS]]


def _skip_rel(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p.startswith(".") or p == "__pycache__" for p in parts):
        return True
    return False
