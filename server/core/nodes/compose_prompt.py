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
            note = ""
            meta = ctx.request.client_meta or {}
            if isinstance(meta.get("reminder_note"), str):
                note = meta["reminder_note"].strip()
            system_parts.append(
                "这是一次「到期提醒」主动推送：事项时间已到，不是用户刚发来的新消息。"
                "请用当前会话 warmth 对应的语气，像当面开口提醒一样说一两句中文；"
                "点明事项，可以带一点关心或俏皮，但不要写成系统通知或「提醒你：」模板句。"
                "禁止再说「已设置／已帮你记下／届时我会提醒」；"
                "禁止调用 create_reminder；禁止追问「还有别的吗」。"
                "若事项需要本机工具才能完成，先办再简短汇报；"
                "纯口头提醒则不要调用工具。"
                + (f"\n到期事项：{note}" if note else "")
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
            if ctx.extras.get("proactive"):
                # Prevent re-scheduling during delivery wake.
                block = {"create_reminder", "list_reminders"}
                tools_payload = [
                    t
                    for t in tools_payload
                    if ((t.get("function") or {}).get("name") not in block)
                ]
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
