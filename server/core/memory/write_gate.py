"""Decide whether a completed turn belongs in the long-term memory inbox.

This is deliberately conservative.  atom-memory still decides which source
becomes an atom, but Velora should not ask it to process every greeting and
one-off question in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryWriteDecision:
    write: bool
    reason: str


_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("identity", ("我叫", "叫我", "我是", "我的名字", "生日", "住在")),
    ("preference", ("我喜欢", "我不喜欢", "我讨厌", "我偏好", "不要叫我", "希望你")),
    ("long_term", ("我的目标", "长期", "计划", "习惯", "以后", "答应", "承诺")),
    ("explicit_memory", ("请记住", "记住：", "记住我", "别忘了")),
)

_SMALL_TALK = {
    "你好",
    "嗨",
    "在吗",
    "谢谢",
    "好的",
    "嗯",
    "哈哈",
    "晚安",
    "早上好",
}


def decide_memory_write(
    user_text: str,
    *,
    is_correction: bool = False,
    proactive: bool = False,
) -> MemoryWriteDecision:
    """Keep only durable, user-centered information in the memory inbox."""
    if is_correction:
        return MemoryWriteDecision(True, "correction")
    if proactive:
        # A reminder is already represented by the structured reminders table;
        # its generated wording should not become a personal fact.
        return MemoryWriteDecision(False, "proactive_delivery")

    text = "".join(user_text.strip().lower().split())
    if not text or text in _SMALL_TALK or len(text) < 4:
        return MemoryWriteDecision(False, "ephemeral_chat")
    for reason, signals in _SIGNALS:
        if any(signal in text for signal in signals):
            return MemoryWriteDecision(True, reason)
    return MemoryWriteDecision(False, "no_durable_signal")
