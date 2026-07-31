from __future__ import annotations

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult


class PostProcessNode:
    """TTS reserved — v0 always SKIP."""

    name = "PostProcess"

    async def process(self, ctx: TurnContext) -> NodeResult:
        del ctx
        return NodeResult.SKIP
