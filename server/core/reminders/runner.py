"""Deliver a due reminder by running the chat chain as an outbound wake."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from server.config import Settings
from server.core.chain.router import TurnRouter
from server.core.chain.types import TurnRequest
from server.core.conversation.store import ConversationStore, ReminderRow
from server.core.reminders.quiet import in_quiet_hours, next_quiet_end_utc
from server.core.reminders.schedule import CronExprError, next_fire_utc
from server.core.reminders.text import finalize_reminder_reply

logger = logging.getLogger("velora.reminders")


async def deliver_reminder(
    reminder: ReminderRow,
    *,
    store: ConversationStore,
    settings: Settings,
    router: TurnRouter,
) -> bool:
    """Return True if handled (delivered, rescheduled, or cron advanced)."""
    now = datetime.now(timezone.utc)
    if settings.quiet_hours_enabled:
        if in_quiet_hours(
            now,
            tz_name=settings.user_timezone,
            start=settings.quiet_hours_start,
            end=settings.quiet_hours_end,
        ):
            new_due = next_quiet_end_utc(
                now,
                tz_name=settings.user_timezone,
                start=settings.quiet_hours_start,
                end=settings.quiet_hours_end,
            )
            await store.reschedule_reminder(reminder.id, new_due)
            logger.info(
                "reminder %s deferred to %s (quiet hours)",
                reminder.id,
                new_due.isoformat(),
            )
            return True

    claimed = await store.claim_due_reminder(reminder.id, now=now)
    if claimed is None:
        logger.info("reminder %s not claimed (already taken)", reminder.id)
        return False

    note = claimed.note
    request = TurnRequest(
        claimed.session_id,
        # Synthetic wake for the LLM only — PersistTurn skips the user bubble.
        user_text="时间到了，请用你一贯的语气当面提醒用户。",
        client_meta={
            "proactive": True,
            "reminder_id": claimed.id,
            "reminder_note": note,
        },
    )
    try:
        ctx = await router.handle(request)
        # Proactive turns must not leave WAIT for the next user message.
        router.wait_registry.clear(claimed.session_id)
        text = finalize_reminder_reply(note, ctx.assistant_text or "")
        kind = (claimed.schedule_kind or "once").lower()
        if kind == "cron" and claimed.cron_expr:
            try:
                nxt = next_fire_utc(
                    claimed.cron_expr,
                    tz_name=settings.user_timezone,
                    after=now,
                )
            except CronExprError as exc:
                await store.mark_reminder_failed(claimed.id, str(exc))
                return False
            if settings.quiet_hours_enabled and in_quiet_hours(
                nxt,
                tz_name=settings.user_timezone,
                start=settings.quiet_hours_start,
                end=settings.quiet_hours_end,
            ):
                nxt = next_quiet_end_utc(
                    nxt,
                    tz_name=settings.user_timezone,
                    start=settings.quiet_hours_start,
                    end=settings.quiet_hours_end,
                )
            await store.complete_cron_run(
                claimed.id, next_due_at=nxt, deliver_text=text
            )
        else:
            await store.mark_reminder_delivered(claimed.id, text)
        logger.info(
            "reminder %s delivered to session %s",
            claimed.id,
            claimed.session_id,
        )
        return True
    except Exception as exc:
        logger.exception("reminder %s failed", claimed.id)
        kind = (claimed.schedule_kind or "once").lower()
        if kind == "cron" and claimed.cron_expr:
            try:
                nxt = next_fire_utc(
                    claimed.cron_expr,
                    tz_name=settings.user_timezone,
                    after=now,
                )
                await store.mark_reminder_failed(
                    claimed.id,
                    str(exc),
                    next_due_at=nxt,
                    keep_pending=True,
                )
            except CronExprError:
                await store.mark_reminder_failed(claimed.id, str(exc))
        else:
            await store.mark_reminder_failed(claimed.id, str(exc))
        return False
