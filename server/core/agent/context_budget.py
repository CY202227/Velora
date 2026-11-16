"""Conservative request sizing and bounded tool-result retention."""

from __future__ import annotations

import json
from typing import Any


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    # UTF-8 byte count deliberately overestimates text; it is not exact tokenization.
    # Non-text inputs need provider-specific accounting and are rejected below.
    for message in messages:
        if isinstance(message.get("content"), list) and any(
            not isinstance(part, dict) or part.get("type") != "text"
            for part in message["content"]
        ):
            raise ValueError("Multimodal context requires provider token accounting")
    return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8")) + 32


def prune_tool_results(messages: list[dict[str, Any]], limit: int = 2000) -> int:
    """Keep call/result structure and recent evidence; change only older results."""
    indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = 0
    for index in indices[:-1]:
        content = messages[index].get("content")
        if isinstance(content, str) and len(content) > limit:
            messages[index] = {**messages[index], "content": content[:limit] + "\n[Earlier tool result truncated]"}
            changed += 1
    return changed
