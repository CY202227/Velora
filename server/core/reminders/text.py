"""Normalize assistant text for reminder delivery wakes."""

from __future__ import annotations

_SETUP_ECHO_MARKERS = (
    "已设置",
    "已为你设置",
    "已帮你设置",
    "已帮你记下",
    "已设提醒",
    "届时我会",
    "还有其他需要",
)


def looks_like_setup_echo(text: str) -> bool:
    """True if the model re-confirmed scheduling instead of delivering."""
    t = (text or "").strip()
    if not t:
        return True
    return any(m in t for m in _SETUP_ECHO_MARKERS)


def finalize_reminder_reply(note: str, text: str) -> str:
    """Prefer the model reply; fall back only if it echoes setup jargon."""
    cleaned = (text or "").strip()
    if cleaned and not looks_like_setup_echo(cleaned):
        return cleaned
    note = (note or "").strip() or "你设的事项"
    # Soft fallback — still natural-ish if the model failed hard.
    return f"嘿，到点了——该{note}啦。"
