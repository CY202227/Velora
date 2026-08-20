from __future__ import annotations

import logging
from typing import Any

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
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

        if not tools_payload or self._tools is None:
            return await self._stream_final(ctx, provider, ctx.messages)

        messages: list[dict[str, Any]] = list(ctx.messages)
        try:
            for round_i in range(self._max_tool_rounds):
                result = await provider.chat(
                    messages, model=ctx.model, tools=tools_payload
                )
                if not result.tool_calls:
                    # Final answer: stream for Desk UX
                    return await self._stream_final(ctx, provider, messages)

                assistant_msg: dict[str, Any] = {
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
                messages.append(assistant_msg)

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

            # Max rounds hit: ask for a plain-text wrap-up without tools
            return await self._stream_final(ctx, provider, messages)
        except Exception as exc:
            logger.exception("LLM tool loop failed")
            ctx.stop_reason = "llm_error"
            await ctx.publish("error", {"message": str(exc)})
            return NodeResult.STOP

    async def _stream_final(
        self,
        ctx: TurnContext,
        provider: OpenAICompatProvider,
        messages: list[dict[str, Any]],
    ) -> NodeResult:
        chunks: list[str] = []
        try:
            async for piece in provider.stream(messages, model=ctx.model):
                chunks.append(piece)
                await ctx.publish("token", {"text": piece})
        except Exception as exc:
            logger.exception("LLM stream failed")
            ctx.stop_reason = "llm_error"
            await ctx.publish("error", {"message": str(exc)})
            return NodeResult.STOP

        ctx.assistant_text = "".join(chunks).strip()
        ctx.messages = messages
        if not ctx.assistant_text:
            ctx.stop_reason = "empty_assistant"
            await ctx.publish("error", {"message": "Empty model response"})
            return NodeResult.STOP
        return NodeResult.CONTINUE
