from __future__ import annotations
from typing import Any
from server.core.chain.context import TurnContext
from server.core.provider.base import ChatResult
from server.core.provider.openai_compat import OpenAICompatProvider

async def stream_round(
    ctx: TurnContext,
    provider: OpenAICompatProvider,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
) -> ChatResult:
    """Stream one provider round; publish token deltas live; return final."""
    stream_chat = getattr(provider, "stream_chat", None)
    if stream_chat is None:
        result = await provider.chat(messages, model=ctx.model, tools=tools)
        if result.tool_calls:
            return result
        text = (result.content or "").strip()
        if not text and hasattr(provider, "stream"):
            chunks: list[str] = []
            async for piece in provider.stream(messages, model=ctx.model):
                chunks.append(piece)
                await ctx.publish("token", {"text": piece})
            return ChatResult(content="".join(chunks), tool_calls=[])
        if text:
            await ctx.publish("token", {"text": text})
        return result

    final: ChatResult | None = None
    async for event in stream_chat(messages, model=ctx.model, tools=tools):
        if event.delta:
            await ctx.publish("token", {"text": event.delta})
        if event.final is not None:
            final = event.final
    return final or ChatResult()
