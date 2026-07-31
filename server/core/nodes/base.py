"""Chain node base."""

from __future__ import annotations

from typing import Protocol

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult


class ChainNode(Protocol):
    name: str

    async def process(self, ctx: TurnContext) -> NodeResult: ...
