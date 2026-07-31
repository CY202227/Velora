"""Background consolidate queue — one space at a time."""

from __future__ import annotations

import asyncio
import logging

from server.core.memory.client import AtomMemoryClient

logger = logging.getLogger(__name__)


class ConsolidateJob:
    def __init__(self, client: AtomMemoryClient) -> None:
        self.client = client
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def start(self) -> None:
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

    async def enqueue(self, space_uid: str, trigger: str = "scheduled") -> None:
        await self._queue.put((space_uid, trigger))

    async def run_now(self, space_uid: str, trigger: str = "correction") -> None:
        lock = self._locks.setdefault(space_uid, asyncio.Lock())
        async with lock:
            await self.client.consolidate(space_uid, trigger=trigger)

    async def _run(self) -> None:
        while True:
            space_uid, trigger = await self._queue.get()
            try:
                await self.run_now(space_uid, trigger=trigger)
            except Exception:
                logger.exception("consolidate job failed for %s", space_uid)
            finally:
                self._queue.task_done()
