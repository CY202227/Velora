"""Thin LLM provider port (AstrBot-shaped, minimal)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string from model


@dataclass
class ChatResult:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> str: ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> AsyncIterator[str]: ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ChatResult: ...
