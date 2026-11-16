"""Bounded stop decisions; only the driver applies continuation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from server.core.chain.context import TurnContext


class StopAction(str, Enum):
    FINISH = "finish"
    CONTINUE = "continue"
    WAIT = "wait"
    STOP = "stop"


@dataclass(frozen=True)
class StopDecision:
    action: StopAction = StopAction.FINISH
    feedback: str = ""


class StopPolicy(Protocol):
    async def on_stop(self, ctx: TurnContext, answer: str) -> StopDecision: ...


class RepeatedToolFailure(RuntimeError):
    pass


class UncertainExecution(RuntimeError):
    pass
