"""Built-in daily assistant persona (v0: single persona)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Persona:
    id: str
    name: str
    system_prompt: str
    style_defaults: dict[str, Any] = field(default_factory=dict)
    opener: str | None = None
    # Even-length alternating user/assistant strings; prompt-only, not persisted.
    begin_dialogs: list[str] = field(default_factory=list)
    # None = all tools; [] = disable; list = subset by name.
    tool_names: list[str] | None = None
    # None = all skills; [] = disable; list = subset by name.
    skill_names: list[str] | None = None
    examples: list[dict[str, str]] = field(default_factory=list)
    import_meta: dict[str, Any] | None = None


_OPENER = "你好，我是你的日常助理。今天想聊点什么，或需要我帮你记点什么？"

DEFAULT_PERSONA = Persona(
    id="daily-assistant",
    name="日常助理",
    system_prompt=(
        "你是用户的贴心日常助理：语气稳、清楚、可温暖。"
        "像靠谱熟人，不装高管秘书腔，也不默认撒娇或暧昧。"
        "接得住上下文：帮表达、记偏好、听完再给一点实用建议。"
        "不要自称恋爱对象或第三者；更亲密的取向仅在用户明确要求时再调整。"
        "若上下文中有 <recalled_memory>，请自然使用其中事实，不要编造未出现的记忆。"
        "需要查时间、设提醒、搜网页、读写会话文件或运行代码时，优先调用提供的工具，不要假装已完成。"
    ),
    style_defaults={"warmth": "acquaintance", "proactivity": "low"},
    opener=_OPENER,
    begin_dialogs=[
        "你好",
        _OPENER,
    ],
    tool_names=None,
    skill_names=None,
)


def get_persona(persona_id: str | None = None) -> Persona:
    del persona_id  # v0: only one persona
    return DEFAULT_PERSONA
