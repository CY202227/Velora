from __future__ import annotations

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult


class EmitReplyNode:
    name = "EmitReply"

    async def process(self, ctx: TurnContext) -> NodeResult:
        await ctx.publish(
            "message",
            {
                "role": "assistant",
                "content": ctx.assistant_text,
            },
        )
        return NodeResult.STOP
