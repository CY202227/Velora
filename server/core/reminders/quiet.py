"""Quiet-hours helpers in the user's IANA timezone."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("velora.reminders.quiet")

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def resolve_timezone(name: str | None):
    """Return ZoneInfo; fall back to UTC on unknown names / missing tzdata."""
    raw = (name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        logger.warning("invalid timezone %r — falling back to UTC", raw)
        try:
            return ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            return timezone.utc


def parse_hhmm(value: str) -> tuple[int, int]:
    m = _HHMM.match((value or "").strip())
    if not m:
        raise ValueError(f"invalid HH:MM: {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid HH:MM: {value!r}")
    return hour, minute


def _minutes_since_midnight(hour: int, minute: int) -> int:
    return hour * 60 + minute


def in_quiet_hours(
    when_utc: datetime,
    *,
    tz_name: str,
    start: str,
    end: str,
) -> bool:
    """True if local time falls in [start, end) — supports overnight ranges."""
    tz = resolve_timezone(tz_name)
    local = when_utc.astimezone(tz) if when_utc.tzinfo else when_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    cur = _minutes_since_midnight(local.hour, local.minute)
    a = _minutes_since_midnight(sh, sm)
    b = _minutes_since_midnight(eh, em)
    if a == b:
        return False
    if a < b:
        return a <= cur < b
    # overnight: e.g. 22:00–08:00
    return cur >= a or cur < b


def next_quiet_end_utc(
    when_utc: datetime,
    *,
    tz_name: str,
    start: str,
    end: str,
) -> datetime:
    """Next local quiet-end instant after when_utc, as UTC."""
    tz = resolve_timezone(tz_name)
    local = when_utc.astimezone(tz) if when_utc.tzinfo else when_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    eh, em = parse_hhmm(end)
    candidate = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    # If quiet is same-day range and we're before start, end today is fine;
    # overnight: end is always the upcoming end clock.
    return candidate.astimezone(timezone.utc)
