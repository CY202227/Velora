"""Single place for persona + memory + history assembly."""

from __future__ import annotations

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.persona.style import clamp_warmth, warmth_instruction


class ComposePromptNode:
    name = "ComposePrompt"

    async def process(self, ctx: TurnContext) -> NodeResult:
        if ctx.persona is None:
            ctx.stop_reason = "no_persona"
            return NodeResult.STOP

        system_parts = [ctx.persona.system_prompt.strip()]
        warmth = clamp_warmth(ctx.style_knobs.get("warmth", 35))
        system_parts.append(warmth_instruction(warmth))
        if ctx.memory_block.strip():
            system_parts.append(
                "以下是与你相关的长期记忆（短断言），请在回答中妥善使用：\n"
                + ctx.memory_block.strip()
            )
        system = "\n\n".join(system_parts)

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for item in ctx.history:
            role = item.get("role")
            content = item.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": ctx.request.user_text})
        ctx.messages = messages
        await ctx.publish(
            "prompt_ready",
            {
                "message_count": len(messages),
                "history_count": max(0, len(messages) - 2),
                "memory_chars": len(ctx.memory_block or ""),
                "warmth": warmth,
                "system_preview": (messages[0].get("content") or "")[:800],
            },
        )
        return NodeResult.CONTINUE
