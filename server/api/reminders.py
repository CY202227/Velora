"""Explicit reminder CRUD (once + cron)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.core.reminders.quiet import in_quiet_hours, next_quiet_end_utc
from server.core.reminders.schedule import CronExprError, next_fire_utc, validate_cron_expr

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


class CreateReminderBody(BaseModel):
    session_id: str
    note: str = Field(min_length=1)
    due_at: str | None = None  # ISO8601; required unless cron_expr
    cron_expr: str | None = None


class ReminderOut(BaseModel):
    id: str
    session_id: str
    note: str
    due_at: str
    status: str
    deliver_text: str | None
    created_at: str
    delivered_at: str | None
    error: str | None
    schedule_kind: str = "once"
    cron_expr: str | None = None
    last_run_at: str | None = None


def _out(row) -> ReminderOut:
    return ReminderOut(
        id=row.id,
        session_id=row.session_id,
        note=row.note,
        due_at=row.due_at,
        status=row.status,
        deliver_text=row.deliver_text,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
        error=row.error,
        schedule_kind=getattr(row, "schedule_kind", None) or "once",
        cron_expr=getattr(row, "cron_expr", None),
        last_run_at=getattr(row, "last_run_at", None),
    )


def _parse_due(raw: str) -> datetime:
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"invalid due_at: {raw}") from exc
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due.astimezone(timezone.utc)


async def _maybe_defer_quiet(request: Request, due: datetime) -> datetime:
    s = request.app.state.velora.settings
    if not s.quiet_hours_enabled:
        return due
    if in_quiet_hours(
        due,
        tz_name=s.user_timezone,
        start=s.quiet_hours_start,
        end=s.quiet_hours_end,
    ):
        return next_quiet_end_utc(
            due,
            tz_name=s.user_timezone,
            start=s.quiet_hours_start,
            end=s.quiet_hours_end,
        )
    return due


@router.get("", response_model=list[ReminderOut])
async def list_reminders(
    request: Request,
    session_id: str | None = None,
    status: str | None = None,
) -> list[ReminderOut]:
    state = request.app.state.velora
    rows = await state.store.list_reminders(session_id, status=status, limit=100)
    return [_out(r) for r in rows]


@router.post("", response_model=ReminderOut)
async def create_reminder(body: CreateReminderBody, request: Request) -> ReminderOut:
    state = request.app.state.velora
    if await state.store.get_session(body.session_id) is None:
        raise HTTPException(404, "session not found")

    cron_raw = (body.cron_expr or "").strip() or None
    schedule_kind = "cron" if cron_raw else "once"
    cron_expr: str | None = None
    if cron_raw:
        try:
            cron_expr = validate_cron_expr(cron_raw)
        except CronExprError as exc:
            raise HTTPException(400, str(exc)) from exc
        due = next_fire_utc(
            cron_expr,
            tz_name=state.settings.user_timezone,
        )
    else:
        if not body.due_at:
            raise HTTPException(400, "due_at or cron_expr required")
        due = _parse_due(body.due_at)

    due = await _maybe_defer_quiet(request, due)
    row = await state.store.create_reminder(
        session_id=body.session_id,
        note=body.note,
        due_at=due,
        schedule_kind=schedule_kind,
        cron_expr=cron_expr,
    )
    return _out(row)


@router.post("/{reminder_id}/cancel", response_model=ReminderOut)
async def cancel_reminder(reminder_id: str, request: Request) -> ReminderOut:
    state = request.app.state.velora
    row = await state.store.cancel_reminder(reminder_id)
    if row is None:
        raise HTTPException(404, "reminder not found")
    return _out(row)
