"""Bounded, non-durable context compaction for a single agent run."""

from __future__ import annotations

from typing import Iterable


def compact_history(
    turns: Iterable[object],
    *,
    max_chars: int,
) -> str:
    """Make a chronological excerpt of older turns without creating a memory.

    This is intentionally extractive rather than an LLM-written summary: it
    cannot silently introduce new facts, and is recomputed per run.  The text
    is later enclosed as untrusted historical data in the prompt.
    """
    remaining = max(0, int(max_chars))
    if remaining <= 0:
        return ""
    lines: list[str] = []
    for turn in turns:
        role = str(getattr(turn, "role", "user") or "user")
        if role not in {"user", "assistant"}:
            continue
        content = " ".join(str(getattr(turn, "content", "") or "").split())
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        prefix = f"{label}："
        room = remaining - len(prefix) - 1
        if room <= 0:
            break
        if len(content) > room:
            lines.append(prefix + content[: max(0, room - 1)] + "…")
            break
        lines.append(prefix + content)
        remaining -= len(prefix) + len(content) + 1
    return "\n".join(lines)
