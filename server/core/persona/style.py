"""Map style_knobs (warmth 0..100) to ComposePrompt instructions."""

from __future__ import annotations

from typing import Any


def clamp_warmth(value: Any, default: int = 35) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, n))


def merge_style_knobs(
    persona_defaults: dict[str, Any] | None,
    session_knobs: dict[str, Any] | None,
    *,
    default_warmth: int = 35,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(persona_defaults or {})
    merged.update(session_knobs or {})
    if "warmth" not in merged:
        # legacy string defaults → mid assistant
        legacy = str(merged.get("warmth", "")).lower()
        if legacy in ("companion", "close", "intimate"):
            merged["warmth"] = 75
        elif legacy in ("assistant", "professional"):
            merged["warmth"] = 20
        elif legacy in ("acquaintance",):
            merged["warmth"] = default_warmth
        else:
            merged["warmth"] = default_warmth
    merged["warmth"] = clamp_warmth(merged["warmth"], default_warmth)
    return merged


def warmth_instruction(warmth: int) -> str:
    """Short style overlay; does not override anti-affair baseline in Persona."""
    w = clamp_warmth(warmth)
    if w <= 25:
        return (
            "风格：更偏日常助理。语气清楚、克制、高效；少闲聊与撒娇；"
            "先办实事，再视需要给简短关心。"
        )
    if w <= 55:
        return (
            "风格：熟人助理。语气稳而可温暖，像靠谱朋友；"
            "接得住情绪，但不主动暧昧或扮演恋爱对象。"
        )
    if w <= 80:
        return (
            "风格：更偏陪伴。语气更亲近、高回应、记得用户偏好；"
            "可以多一点情感支持，仍保持尊重边界，不默认恋爱/第三者叙事。"
        )
    return (
        "风格：亲密陪伴（用户已显式调高）。语气更柔软、偏爱感更强；"
        "仍禁止鼓励隐瞒现实伴侣或「小三」叙事；用户未要求时不要自称恋人。"
    )
