from __future__ import annotations

import logging

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.provider.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


class LLMNode:
    name = "LLM"

    def __init__(self, provider: OpenAICompatProvider | None = None) -> None:
        self._provider = provider

    def _provider_for(self, ctx: TurnContext) -> OpenAICompatProvider:
        if self._provider is not None:
            return self._provider
        return OpenAICompatProvider(ctx.llm_base_url, ctx.llm_api_key)

    async def process(self, ctx: TurnContext) -> NodeResult:
        if not ctx.llm_api_key and "localhost" not in ctx.llm_base_url and "127.0.0.1" not in ctx.llm_base_url:
            # Allow local endpoints without key; cloud needs key.
            if not ctx.llm_base_url:
                ctx.stop_reason = "llm_not_configured"
                await ctx.publish("error", {"message": "LLM not configured"})
                return NodeResult.STOP

        provider = self._provider_for(ctx)
        chunks: list[str] = []
        try:
            async for piece in provider.stream(ctx.messages, model=ctx.model):
                chunks.append(piece)
                await ctx.publish("token", {"text": piece})
        except Exception as exc:
            logger.exception("LLM stream failed")
            ctx.stop_reason = "llm_error"
            await ctx.publish("error", {"message": str(exc)})
            return NodeResult.STOP

        ctx.assistant_text = "".join(chunks).strip()
        if not ctx.assistant_text:
            ctx.stop_reason = "empty_assistant"
            await ctx.publish("error", {"message": "Empty model response"})
            return NodeResult.STOP
        return NodeResult.CONTINUE
