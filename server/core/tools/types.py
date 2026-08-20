"""Builtin tool specifications (OpenAI function schema)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from server.core.chain.context import TurnContext

# Handler returns a string (usually JSON) for the tool role message.
ToolHandler = Callable[[dict[str, Any], TurnContext], Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    handler: ToolHandler | None = None

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
