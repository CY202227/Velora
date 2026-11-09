"""Background consolidate queue — one space at a time."""

from __future__ import annotations

import asyncio
import logging
import re

from server.core.memory.client import AtomMemoryClient

logger = logging.getLogger(__name__)


class ConsolidateJob:
    def __init__(
        self,
        client: AtomMemoryClient,
        *,
        layer_every_n: int = 5,
    ) -> None:
        self.client = client
        self.layer_every_n = layer_every_n
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._model_slot = asyncio.Lock()
        self._scheduled_done = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            self._retry_task = asyncio.create_task(self._retry())

    async def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def enqueue(self, space_uid: str, trigger: str = "scheduled") -> None:
        await self._queue.put((space_uid, trigger))

    async def run_now(self, space_uid: str, trigger: str = "correction", *, source_id: int | None = None) -> bool:
        lock = self._locks.setdefault(space_uid, asyncio.Lock())
        async with lock, self._model_slot:
            result = await self.client.consolidate(space_uid, trigger=trigger)
            if result and result.get("status") == "failed":
                code = self._log_failure(result, trigger)
                sources = result.get("source_ids") or []
                if code in {"timeout", "output_truncated"} and len(sources) > 1:
                    result = await self.client.consolidate(
                        space_uid, trigger=trigger, max_sources=min(10, len(sources) // 2),
                    )
                    if result and result.get("status") == "failed":
                        self._log_failure(result, trigger)
            if not result or result.get("status") != "succeeded":
                return False
            await self._maybe_run_layers(space_uid, trigger)
            if source_id is not None:
                source = await self.client.get_source(space_uid, source_id)
                return bool(source and source.get("status") == "consolidated")
            return True

    @staticmethod
    def _log_failure(result: dict, trigger: str) -> str:
        match = re.match(r"LLMError\[([a-z_]+)\]", str(result.get("error") or ""))
        code = match.group(1) if match else "unclassified"
        logger.warning("memory consolidation failed: trigger=%s run=%s sources=%d code=%s",
                       trigger, result.get("id"), len(result.get("source_ids") or []), code)
        return code

    async def _maybe_run_layers(self, space_uid: str, trigger: str) -> None:
        """L2/L3 stay off the L1 path; only a slower scheduled cadence."""
        if trigger != "scheduled" or self.layer_every_n <= 0:
            return
        self._scheduled_done += 1
        if self._scheduled_done % self.layer_every_n != 0:
            return
        await self.client.synthesize(space_uid)
        await self.client.persona(space_uid)

    async def _run(self) -> None:
        while True:
            space_uid, trigger = await self._queue.get()
            try:
                await self.run_now(space_uid, trigger=trigger)
            except Exception:
                logger.exception("consolidate job failed for %s", space_uid)
            finally:
                self._queue.task_done()


    async def _retry(self) -> None:
        while True:
            try:
                await self.client.retry_deliveries(self)
            except Exception:
                logger.exception("memory delivery retry failed")
            await asyncio.sleep(30)
