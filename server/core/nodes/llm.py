from __future__ import annotations

import logging
from typing import Any

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.provider.base import ChatResult
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.tools.registry import ToolRegistry
from server.core.tools.runner import execute_tool

logger = logging.getLogger(__name__)


class LLMNode:
    name = "LLM"

    def __init__(
        self,
        provider: OpenAICompatProvider | None = None,
        *,
        tools: ToolRegistry | None = None,
        max_tool_rounds: int = 4,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._max_tool_rounds = max(1, int(max_tool_rounds))

    def _provider_for(self, ctx: TurnContext) -> OpenAICompatProvider:
        if self._provider is not None:
            return self._provider
        return OpenAICompatProvider(ctx.llm_base_url, ctx.llm_api_key)

    async def process(self, ctx: TurnContext) -> NodeResult:
        if not ctx.llm_api_key and "localhost" not in ctx.llm_base_url and "127.0.0.1" not in ctx.llm_base_url:
            if not ctx.llm_base_url:
                ctx.stop_reason = "llm_not_configured"
                await ctx.publish("error", {"message": "LLM not configured"})
                return NodeResult.STOP

        provider = self._provider_for(ctx)
        tools_payload: list[dict[str, Any]] = list(ctx.extras.get("tools") or [])
        use_tools = bool(tools_payload) and self._tools is not None
        messages: list[dict[str, Any]] = list(ctx.messages)

        try:
            for round_i in range(self._max_tool_rounds):
                # One streaming request includes tools. A plain reply
                # (no tool_calls) finishes in that same request — no second generate.
                round_tools = tools_payload if use_tools else None
                if round_i == self._max_tool_rounds - 1:
                    round_tools = None

                result = await self._stream_round(
                    ctx, provider, messages, tools=round_tools
                )
                if not result.tool_calls:
                    ctx.assistant_text = (result.content or "").strip()
                    ctx.messages = messages
                    if not ctx.assistant_text:
                        ctx.stop_reason = "empty_assistant"
                        await ctx.publish(
                            "error", {"message": "Empty model response"}
                        )
                        return NodeResult.STOP
                    return NodeResult.CONTINUE

                if self._tools is None:
                    ctx.stop_reason = "llm_error"
                    await ctx.publish(
                        "error",
                        {"message": "Model requested tools but none registered"},
                    )
                    return NodeResult.STOP

                # Providers can stream a natural-language preamble before the
                # final chunk reveals tool_calls. That text is a draft, not the
                # answer: tell clients to clear it before we execute actions,
                # otherwise the eventual answer gets glued to stale narration.
                await ctx.publish(
                    "assistant_draft_reset",
                    {"reason": "tool_call", "round": round_i},
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": result.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                },
                            }
                            for tc in result.tool_calls
                        ],
                    }
                )

                for tc in result.tool_calls:
                    await ctx.publish(
                        "tool_call",
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "round": round_i,
                        },
                    )
                    out = await execute_tool(
                        self._tools, tc.name, tc.arguments, ctx
                    )
                    await ctx.publish(
                        "tool_result",
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "result": out[:2000],
                            "round": round_i,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": out,
                        }
                    )

            ctx.stop_reason = "empty_assistant"
            await ctx.publish(
                "error", {"message": "Tool loop exhausted without a final reply"}
            )
            return NodeResult.STOP
        except Exception as exc:
            logger.exception("LLM tool loop failed")
            ctx.stop_reason = "llm_error"
            await ctx.publish("error", {"message": str(exc)})
            return NodeResult.STOP

    async def _stream_round(
        self,
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
