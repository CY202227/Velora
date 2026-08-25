"""Per-session workspace rooted under velora_data/workspaces."""

from __future__ import annotations

import re
from pathlib import Path

_SAFE_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class WorkspaceError(ValueError):
    """Invalid or escaping path."""


def workspace_root(workspaces_dir: str | Path, session_id: str) -> Path:
    if not _SAFE_SESSION.match(session_id or ""):
        # UUID is safe; sanitize otherwise
        sid = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")[:80]
    else:
        sid = session_id
    root = (Path(workspaces_dir) / sid).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_in_workspace(
    workspaces_dir: str | Path,
    session_id: str,
    path: str | None,
    *,
    allow_abs: bool = False,
    extra_roots: list[str | Path] | None = None,
) -> Path:
    """Resolve path under session workspace (or allowlisted extra roots)."""
    root = workspace_root(workspaces_dir, session_id)
    raw = (path or ".").strip() or "."
    candidate = Path(raw)
    extras = [Path(p).resolve() for p in (extra_roots or [])]

    if candidate.is_absolute():
        resolved = candidate.resolve()
        if allow_abs:
            return resolved
        if any(_under(resolved, er) for er in extras):
            return resolved
        if _under(resolved, root):
            return resolved
        raise WorkspaceError("absolute paths are not allowed")

    resolved = (root / candidate).resolve()
    if not _under(resolved, root):
        raise WorkspaceError(f"path escapes workspace: {raw}")
    return resolved
