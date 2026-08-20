"""Parse short-form reminder lines from chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ParsedReminder:
    note: str
    due_at: datetime


def parse_reminder_line(text: str) -> ParsedReminder | None:
    """Parse `请提醒：事项 | 2026-08-06T15:00:00+08:00`."""
    raw = (text or "").strip()
    if not raw.startswith("请提醒：") and not raw.startswith("请提醒:"):
        return None
    body = raw.split("：", 1)[-1] if "：" in raw else raw.split(":", 1)[-1]
    body = body.strip()
    if "|" not in body:
        return None
    note_part, due_part = body.rsplit("|", 1)
    note = note_part.strip()
    due_raw = due_part.strip()
    if not note or not due_raw:
        return None
    try:
        due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return ParsedReminder(note=note, due_at=due.astimezone(timezone.utc))
