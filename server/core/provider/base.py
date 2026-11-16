"""Small provider interface for streaming model responses and tool calls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string from model


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @classmethod
    def from_payload(cls, value: Any) -> TokenUsage | None:
        if not isinstance(value, dict):
            return None
        def number(raw):
            return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
        details = value.get("prompt_tokens_details") or value.get("input_tokens_details") or {}
        return cls(
            number(value.get("prompt_tokens", value.get("input_tokens"))),
            number(value.get("completion_tokens", value.get("output_tokens"))),
            number(details.get("cached_tokens")) if isinstance(details, dict) else 0,
        )


@dataclass
class ChatResult:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    finish_reason: str | None = None


@dataclass
class StreamEvent:
    """One piece of a streaming chat turn.

    Deltas carry text; the final event carries the assembled ChatResult
    (content + tool_calls). Exactly one final event is emitted per request.
    """

    delta: str | None = None
    final: ChatResult | None = None

    @property
    def is_final(self) -> bool:
        return self.final is not None


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

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
    ) -> ChatResult: ...
