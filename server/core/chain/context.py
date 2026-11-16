"""Mutable per-turn execution context shared across chain nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from server.core.agent.state import RunState

from server.core.chain.types import StreamEvent, TurnRequest
from server.core.persona.default import Persona


EmitFn = Callable[[StreamEvent], Awaitable[None]]


@dataclass
class TurnContext:
    request: TurnRequest
    session_id: str
    persona: Persona | None = None
    memory_space_uid: str = "velora-default"
    memory_block: str = ""
    history: list[dict[str, str]] = field(default_factory=list)
    history_compaction: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    assistant_text: str = ""
    model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    tts_enabled: bool = False
    style_knobs: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    is_correction: bool = False
    memory_wrote: bool = False
    consolidated: bool = False
    stop_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    emit: EmitFn | None = None
    agent_run: RunState | None = None
    checkpoint: Callable[[str], Awaitable[None]] | None = None

    async def save_checkpoint(self, boundary: str) -> None:
        if self.checkpoint is not None:
            await self.checkpoint(boundary)

    async def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        if self.emit is not None:
            await self.emit(StreamEvent(event_type, data))
