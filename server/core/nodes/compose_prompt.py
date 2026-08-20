"""Single place for persona + memory + history assembly."""

from __future__ import annotations

from typing import Any

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.persona.style import clamp_warmth, warmth_instruction
from server.core.skills.manager import SkillManager
from server.core.tools.registry import ToolRegistry


class ComposePromptNode:
    name = "ComposePrompt"

    def __init__(
        self,
        tools: ToolRegistry | None = None,
        skills: SkillManager | None = None,
    ) -> None:
        self._tools = tools
        self._skills = skills

    async def process(self, ctx: TurnContext) -> NodeResult:
        if ctx.persona is None:
            ctx.stop_reason = "no_persona"
            return NodeResult.STOP

        system_parts = [ctx.persona.system_prompt.strip()]
        warmth = clamp_warmth(ctx.style_knobs.get("warmth", 35))
        system_parts.append(warmth_instruction(warmth))
        if ctx.extras.get("proactive"):
            system_parts.append(
                "这是一次定时任务唤醒，不是用户刚发来的闲聊。"
                "请按事项执行：可用工具完成工作，少寒暄，不要连环追问。"
                "产出文件必须写在当前会话 workspace；办完后直接向用户汇报结果。"
            )
        if ctx.memory_block.strip():
            system_parts.append(
                "以下是与你相关的长期记忆（短断言），请在回答中妥善使用：\n"
                + ctx.memory_block.strip()
            )
        skills_count = 0
        if self._skills is not None:
            skills_block = self._skills.build_skills_prompt(ctx.persona.skill_names)
            if skills_block:
                system_parts.append(skills_block)
                skills_count = len(self._skills.list_skills(ctx.persona.skill_names))
        system = "\n\n".join(system_parts)

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

        begin = list(ctx.persona.begin_dialogs or [])
        if len(begin) % 2 == 1:
            begin = begin[:-1]
        for i in range(0, len(begin), 2):
            u, a = begin[i].strip(), begin[i + 1].strip()
            if u:
                messages.append({"role": "user", "content": u})
            if a:
                messages.append({"role": "assistant", "content": a})

        for item in ctx.history:
            role = item.get("role")
            content = item.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": ctx.request.user_text})
        ctx.messages = messages

        tools_payload: list[dict[str, Any]] = []
        if self._tools is not None:
            tools_payload = self._tools.openai_tools(ctx.persona.tool_names)
        ctx.extras["tools"] = tools_payload

        await ctx.publish(
            "prompt_ready",
            {
                "message_count": len(messages),
                "history_count": max(0, len(ctx.history)),
                "memory_chars": len(ctx.memory_block or ""),
                "warmth": warmth,
                "tools_count": len(tools_payload),
                "skills_count": skills_count,
                "system_preview": (messages[0].get("content") or "")[:800],
            },
        )
        return NodeResult.CONTINUE
