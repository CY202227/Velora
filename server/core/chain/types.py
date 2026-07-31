"""Chain node result codes — inspired by AstrBot #1948 Chain, not onion yield."""

from __future__ import annotations

from enum import Enum
from typing import Any


class NodeResult(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"
    WAIT = "wait"
    SKIP = "skip"


class TurnRequest:
    __slots__ = ("session_id", "user_text", "persona_id", "client_meta")

    def __init__(
        self,
        session_id: str,
        user_text: str,
        persona_id: str | None = None,
        client_meta: dict[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.user_text = user_text
        self.persona_id = persona_id
        self.client_meta = client_meta or {}


class StreamEvent:
    """SSE-facing event emitted during a turn."""

    __slots__ = ("type", "data")

    def __init__(self, type: str, data: dict[str, Any] | None = None) -> None:
        self.type = type
        self.data = data or {}
