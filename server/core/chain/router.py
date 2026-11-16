"""TurnRouter: new turn vs resume from WaitRegistry."""

from __future__ import annotations

import asyncio
import logging
from server.core.agent.result import terminal_result
from collections import deque

from server.core.chain.context import EmitFn, TurnContext
from server.core.chain.executor import ChainExecutor
from server.core.chain.locks import SessionLockManager
from server.core.chain.types import TurnRequest
from server.core.chain.wait_registry import WaitRegistry


class TurnRouter:
    def __init__(
        self,
        executor: ChainExecutor,
        locks: SessionLockManager,
        wait_registry: WaitRegistry,
        run_store=None,
        execution_store=None,
        approvals=None,
        snapshots=None,
    ) -> None:
        self._active: dict[str, asyncio.Task] = {}
        self._contexts: dict[str, TurnContext] = {}
        self._recent = deque(maxlen=50)
        self._cancel_requested: set[str] = set()
        self.run_store = run_store
        self.execution_store = execution_store
        self.approvals = approvals
        self.snapshots = snapshots
        self.executor = executor
        self.locks = locks
        self.wait_registry = wait_registry

    @staticmethod
    def _snapshot(ctx: TurnContext) -> dict:
        run = ctx.agent_run
        return {
            "session_id": ctx.session_id,
            "runtime_mode": run.runtime_mode if run else "pending",
            "phase": run.phase.value if run else "preparing",
            "status": "cancelled" if ctx.stop_reason == "cancelled" else (run.outcome.status.value if run and run.outcome else "running"),
            "reason": ctx.stop_reason,
            "step": run.step if run else 0,
            "input_tokens": run.input_tokens if run else 0,
            "output_tokens": run.output_tokens if run else 0,
            "context_tokens": run.estimated_input_tokens if run else 0,
            "count_source": run.token_count_source if run else "estimate",
            "retries": run.retries if run else 0,
            "compactions": run.compactions if run else 0,
            "tools": [{k: r[k] for k in ("id", "name", "status", "round")} for r in run.tool_executions] if run else [],
        }

    def runtime_status(self) -> dict:
        return {"active": [self._snapshot(ctx) for ctx in self._contexts.values()], "recent": list(self._recent)}

    def is_running(self, session_id: str) -> bool:
        task = self._active.get(session_id)
        return task is not None and not task.done()

    def cancel(self, session_id: str, *, owner_task=None) -> bool:
        """Signal the active turn directly, without acquiring its session lock."""
        task = self._active.get(session_id)
        if owner_task is not None and task is not owner_task:
            return False
        if task is None or task.done() or session_id in self._cancel_requested:
            return False
        self._cancel_requested.add(session_id)
        task.cancel()
        return True

    async def handle(
        self,
        request: TurnRequest,
        *,
        emit: EmitFn | None = None,
        base_ctx: TurnContext | None = None,
        resume_run_id: str | None = None,
    ) -> TurnContext:
        lock = await self.locks.acquire(request.session_id)
        async with lock:
            wait = self.wait_registry.get(request.session_id)
            start_at = 0
            if wait is not None:
                start_at = int(wait.payload.get("next_index", 0))
                self.wait_registry.clear(request.session_id)

            if base_ctx is None:
                ctx = TurnContext(
                    request=request,
                    session_id=request.session_id,
                    emit=emit,
                )
            else:
                ctx = base_ctx
                ctx.request = request
                ctx.emit = emit

            if self.execution_store is not None:
                ctx.extras["execution_store"] = self.execution_store
            if self.approvals is not None:
                ctx.extras["approvals"] = self.approvals
            if self.snapshots is not None:
                ctx.extras["snapshots"] = self.snapshots
            if resume_run_id is not None:
                if self.run_store is None:
                    raise ValueError("Run persistence is unavailable")
                source = await self.run_store.read(resume_run_id)
                if source is None or source["session_id"] != request.session_id:
                    raise ValueError("Run checkpoint does not match the session")
                ctx.extras["resume_checkpoint"] = await self.run_store.claim(resume_run_id, request.user_text)
                ctx.request = TurnRequest(request.session_id, source["user_text"])
            if self.run_store is not None:
                async def checkpoint(boundary):
                    await self.run_store.checkpoint(ctx, boundary)
                ctx.checkpoint = checkpoint
            await ctx.publish("turn_start", {"session_id": request.session_id})
            task = asyncio.current_task()
            self._active[request.session_id] = task
            self._contexts[request.session_id] = ctx
            try:
                ctx = await self.executor.execute(ctx, start_at=start_at)
            except asyncio.CancelledError:
                ctx.stop_reason = "cancelled"
                if request.session_id not in self._cancel_requested:
                    raise
            finally:
                try:
                    await ctx.save_checkpoint("chain_exit")
                except Exception:
                    ctx.stop_reason = "checkpoint_failed"
                    logging.getLogger(__name__).exception("Run checkpoint could not be committed")
                finally:
                    self._recent.appendleft(self._snapshot(ctx))
                    self._contexts.pop(request.session_id, None)
                    self._active.pop(request.session_id, None)
                    self._cancel_requested.discard(request.session_id)
            await ctx.publish(
                "turn_end",
                {
                    "session_id": request.session_id,
                    "assistant_text": ctx.assistant_text,
                    "stop_reason": ctx.stop_reason,
                    "memory_wrote": ctx.memory_wrote,
                    "result": terminal_result(ctx),
                },
            )
            return ctx
