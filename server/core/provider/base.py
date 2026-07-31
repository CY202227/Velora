"""Thin LLM provider port (AstrBot-shaped, minimal)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class ChatProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
    ) -> str: ...

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
    ) -> AsyncIterator[str]: ...
