"""Linear ChainExecutor: walk nodes, honor NodeResult codes."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.chain.wait_registry import WaitRegistry, WaitState
from server.core.nodes.base import ChainNode

logger = logging.getLogger(__name__)


class ChainExecutor:
    def __init__(
        self,
        nodes: Sequence[ChainNode],
        wait_registry: WaitRegistry | None = None,
    ) -> None:
        self.nodes = list(nodes)
        self.wait_registry = wait_registry or WaitRegistry()

    async def execute(self, ctx: TurnContext, *, start_at: int = 0) -> TurnContext:
        i = start_at
        while i < len(self.nodes):
            node = self.nodes[i]
            name = node.name
            try:
                result = await node.process(ctx)
            except Exception:
                logger.exception("node %s failed", name)
                ctx.stop_reason = f"node_error:{name}"
                await ctx.publish("error", {"message": f"Node {name} failed"})
                return ctx

            if result is NodeResult.SKIP:
                i += 1
                continue
            if result is NodeResult.CONTINUE:
                i += 1
                continue
            if result is NodeResult.STOP:
                return ctx
            if result is NodeResult.WAIT:
                self.wait_registry.set(
                    WaitState(
                        session_id=ctx.session_id,
                        node_name=name,
                        payload={"next_index": i + 1},
                    )
                )
                await ctx.publish("wait", {"node": name})
                return ctx
            i += 1
        return ctx
