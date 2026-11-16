"""Per-invocation model execution state; not a durable task checkpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4
from typing import Any


class RunPhase(str, Enum):
    PREPARING = "preparing"
    REQUESTING = "requesting"
    COMPACTING = "compacting"
    EXECUTING_TOOLS = "executing_tools"
    AWAITING_APPROVAL = "awaiting_approval"
    CHECKING_STOP = "checking_stop"


class RunStatus(str, Enum):
    # Completion describes the model stage, not verified business-task success.
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus
    reason: str | None = None


@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: uuid4().hex)
    runtime_mode: str = "unified"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    latest_input_tokens: int | None = None
    estimated_input_tokens: int = 0
    token_count_source: str = "estimate"
    context_window: int | None = None
    retries: int = 0
    stop_checks: int = 0
    forced_continuations: int = 0
    output_recoveries: int = 0
    context_recoveries: int = 0
    answer_prefix: str = ""
    acceptance: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    failure_fingerprint: str | None = None
    consecutive_tool_failures: int = 0
    tool_executions: list[dict[str, Any]] = field(default_factory=list)
    pruned_results: int = 0
    compaction_attempts: int = 0
    compactions: int = 0
    context_archive: list[list[dict[str, Any]]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    phase: RunPhase = RunPhase.PREPARING
    step: int = 0
    outcome: RunOutcome | None = None

    def enter(self, phase: RunPhase) -> None:
        if self.outcome is not None:
            raise RuntimeError("Cannot advance a finished model run")
        self.phase = phase

    def finish(self, status: RunStatus, reason: str | None = None) -> RunOutcome:
        if self.outcome is not None:
            raise RuntimeError("Model run already finished")
        self.outcome = RunOutcome(status, reason)
        return self.outcome
