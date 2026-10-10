"""In-memory registry for turns paused by a chain node."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WaitState:
    session_id: str
    node_name: str
    payload: dict


class WaitRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, WaitState] = {}

    def get(self, session_id: str) -> WaitState | None:
        return self._pending.get(session_id)

    def set(self, state: WaitState) -> None:
        self._pending[state.session_id] = state

    def clear(self, session_id: str) -> None:
        self._pending.pop(session_id, None)
