"""Cron schedule helpers for recurring reminders."""

from __future__ import annotations

from datetime import datetime, timezone

from croniter import croniter

from server.core.reminders.quiet import resolve_timezone


class CronExprError(ValueError):
    """Invalid five-field cron expression."""


def validate_cron_expr(expr: str) -> str:
    raw = (expr or "").strip()
    parts = raw.split()
    if len(parts) != 5:
        raise CronExprError("cron must have 5 fields: min hour dom mon dow")
    try:
        croniter(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CronExprError(f"invalid cron: {raw}") from exc
    return raw


def next_fire_utc(
    cron_expr: str,
    *,
    tz_name: str,
    after: datetime | None = None,
) -> datetime:
    """Return the next fire time as UTC after `after` (default now)."""
    expr = validate_cron_expr(cron_expr)
    tz = resolve_timezone(tz_name)
    base = after or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    local_base = base.astimezone(tz)
    itr = croniter(expr, local_base)
    nxt = itr.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    else:
        nxt = nxt.astimezone(tz)
    return nxt.astimezone(timezone.utc)
