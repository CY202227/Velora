"""SillyTavern / chara card macro substitution."""

from __future__ import annotations


def apply_macros(
    text: str,
    *,
    char_name: str,
    user_name: str = "用户",
    original: str | None = None,
) -> str:
    """Replace {{char}} / {{user}} / {{original}} placeholders."""
    if not text:
        return text
    out = text.replace("{{char}}", char_name).replace("{{Char}}", char_name)
    out = out.replace("{{user}}", user_name).replace("{{User}}", user_name)
    if original is not None and "{{original}}" in out:
        out = out.replace("{{original}}", original)
    return out
