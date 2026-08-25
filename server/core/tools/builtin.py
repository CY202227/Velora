"""Built-in tools: time, reminders, memory summary."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from server.core.chain.context import TurnContext
from server.core.memory.labels import build_memory_summary
from server.core.reminders.quiet import (
    in_quiet_hours,
    next_quiet_end_utc,
    resolve_timezone,
)
from server.core.tools.registry import ToolRegistry
from server.core.tools.types import ToolSpec

logger = logging.getLogger("velora.tools")


def _settings(ctx: TurnContext):
    return ctx.extras.get("settings")


def _store(ctx: TurnContext):
    return ctx.extras.get("store")


def _memory(ctx: TurnContext):
    return ctx.extras.get("memory")


async def _get_current_time(args: dict[str, Any], ctx: TurnContext) -> str:
    del args
    settings = _settings(ctx)
    tz_name = getattr(settings, "user_timezone", None) or "UTC"
    tz = resolve_timezone(tz_name)
    now = datetime.now(timezone.utc).astimezone(tz)
    return json.dumps(
        {
            "timezone": str(tz),
            "local": now.strftime("%Y-%m-%d %H:%M:%S"),
            "iso": now.isoformat(),
            "utc": datetime.now(timezone.utc).isoformat(),
        },
        ensure_ascii=False,
    )


async def _create_reminder(args: dict[str, Any], ctx: TurnContext) -> str:
    store = _store(ctx)
    settings = _settings(ctx)
    if store is None or settings is None:
        return json.dumps({"ok": False, "error": "store/settings unavailable"})
    note = str(args.get("note") or "").strip()
    due_raw = str(args.get("due_at") or "").strip()
    cron_raw = str(args.get("cron_expression") or "").strip()
    if not note:
        return json.dumps({"ok": False, "error": "note required"})
    if not due_raw and not cron_raw:
        return json.dumps({"ok": False, "error": "due_at or cron_expression required"})

    from server.core.reminders.schedule import (
        CronExprError,
        next_fire_utc,
        validate_cron_expr,
    )

    schedule_kind = "once"
    cron_expr: str | None = None
    if cron_raw:
        try:
            cron_expr = validate_cron_expr(cron_raw)
        except CronExprError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        schedule_kind = "cron"
        due = next_fire_utc(cron_expr, tz_name=settings.user_timezone)
    else:
        try:
            due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
        except ValueError:
            return json.dumps({"ok": False, "error": f"invalid due_at: {due_raw}"})
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        due = due.astimezone(timezone.utc)

    if getattr(settings, "quiet_hours_enabled", False) and in_quiet_hours(
        due,
        tz_name=settings.user_timezone,
        start=settings.quiet_hours_start,
        end=settings.quiet_hours_end,
    ):
        due = next_quiet_end_utc(
            due,
            tz_name=settings.user_timezone,
            start=settings.quiet_hours_start,
            end=settings.quiet_hours_end,
        )
    row = await store.create_reminder(
        session_id=ctx.session_id,
        note=note,
        due_at=due,
        schedule_kind=schedule_kind,
        cron_expr=cron_expr,
    )
    return json.dumps(
        {
            "ok": True,
            "id": row.id,
            "note": row.note,
            "due_at": row.due_at,
            "status": row.status,
            "schedule_kind": row.schedule_kind,
            "cron_expr": row.cron_expr,
        },
        ensure_ascii=False,
    )


async def _list_reminders(args: dict[str, Any], ctx: TurnContext) -> str:
    del args
    store = _store(ctx)
    if store is None:
        return json.dumps({"ok": False, "error": "store unavailable"})
    rows = await store.list_reminders(
        ctx.session_id, status="pending", limit=20
    )
    return json.dumps(
        {
            "ok": True,
            "count": len(rows),
            "reminders": [
                {
                    "id": r.id,
                    "note": r.note,
                    "due_at": r.due_at,
                    "status": r.status,
                    "schedule_kind": r.schedule_kind,
                    "cron_expr": r.cron_expr,
                }
                for r in rows
            ],
        },
        ensure_ascii=False,
    )


async def _memory_summary(args: dict[str, Any], ctx: TurnContext) -> str:
    del args
    memory = _memory(ctx)
    settings = _settings(ctx)
    if memory is None:
        return json.dumps({"ok": False, "error": "memory client unavailable"})
    uid = ctx.memory_space_uid or getattr(settings, "memory_space_uid", "") or ""
    try:
        data = await memory.list_atoms(uid, page=1, page_size=100)
        results = data.get("results") if isinstance(data, dict) else None
        if results is None and isinstance(data, list):
            results = data
        summary = build_memory_summary(results or [])
        return json.dumps({"ok": True, **summary}, ensure_ascii=False)
    except Exception as exc:
        logger.warning("memory_summary tool failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc)})


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="get_current_time",
            description="获取用户时区下的当前本地时间。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_get_current_time,
        )
    )
    registry.register(
        ToolSpec(
            name="create_reminder",
            description=(
                "为当前会话创建一条显式提醒或定时任务。"
                "一次性：传 due_at（ISO8601，建议带时区）。"
                "周期：传 cron_expression（五段 crontab，按用户时区）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "提醒/任务事项原文（到期后执行并汇报）",
                    },
                    "due_at": {
                        "type": "string",
                        "description": "一次性到期时间 ISO8601",
                    },
                    "cron_expression": {
                        "type": "string",
                        "description": "五段 crontab，如 0 9 * * 1-5",
                    },
                },
                "required": ["note"],
                "additionalProperties": False,
            },
            handler=_create_reminder,
        )
    )
    registry.register(
        ToolSpec(
            name="list_reminders",
            description="列出当前会话尚未投递的待办提醒。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_list_reminders,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_summary",
            description="只读：获取长期记忆摘要（用户偏好与事实概览）。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_memory_summary,
        )
    )
