"""Background poller for due reminders."""

from __future__ import annotations

import asyncio
import logging

from server.config import Settings
from server.core.chain.router import TurnRouter
from server.core.conversation.store import ConversationStore
from server.core.reminders.runner import deliver_reminder

logger = logging.getLogger("velora.reminders")


class ReminderJob:
    def __init__(
        self,
        store: ConversationStore,
        settings: Settings,
        router: TurnRouter,
    ) -> None:
        self.store = store
        self.settings = settings
        self.router = router
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.settings.reminders_enabled:
            logger.info("reminders disabled — ReminderJob not started")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        interval = max(5, int(self.settings.reminder_poll_seconds))
        while True:
            try:
                due = await self.store.list_due_reminders(limit=5)
                for rem in due:
                    await deliver_reminder(
                        rem,
                        store=self.store,
                        settings=self.settings,
                        router=self.router,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reminder poll loop error")
            await asyncio.sleep(interval)
