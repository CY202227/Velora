"""Dangerous command policy for local shell."""

from __future__ import annotations

import re

_DENIED_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/\s*$",
        r"\brm\s+-rf\s+/",
        r"\bdel\s+/f\s+/s\s+",
        r"\brd\s+/s\s+/q\s+",
        r"\bformat\s+",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bpowershell[^;|&]*-enc(odedcommand)?\b",
        r"\bcurl\s+[^\n]*\|\s*(ba)?sh\b",
        r"\bwget\s+[^\n]*\|\s*(ba)?sh\b",
        r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",  # fork bomb
    )
]


def check_shell_command(command: str) -> str | None:
    """Return error message if denied, else None."""
    text = (command or "").strip()
    if not text:
        return "empty command"
    for pat in _DENIED_PATTERNS:
        if pat.search(text):
            return f"command blocked by policy: {text[:120]}"
    return None
