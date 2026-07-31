"""TurnRouter: new turn vs resume from WaitRegistry."""

from __future__ import annotations

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
    ) -> None:
        self.executor = executor
        self.locks = locks
        self.wait_registry = wait_registry

    async def handle(
        self,
        request: TurnRequest,
        *,
        emit: EmitFn | None = None,
        base_ctx: TurnContext | None = None,
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

            await ctx.publish("turn_start", {"session_id": request.session_id})
            ctx = await self.executor.execute(ctx, start_at=start_at)
            await ctx.publish(
                "turn_end",
                {
                    "session_id": request.session_id,
                    "assistant_text": ctx.assistant_text,
                    "stop_reason": ctx.stop_reason,
                    "memory_wrote": ctx.memory_wrote,
                },
            )
            return ctx
