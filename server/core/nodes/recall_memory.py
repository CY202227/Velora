from __future__ import annotations

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.memory.client import AtomMemoryClient


class RecallMemoryNode:
    name = "RecallMemory"

    def __init__(
        self,
        client: AtomMemoryClient,
        *,
        max_atoms: int = 5,
        budget_chars: int = 400,
        policy: str = "layered",
    ) -> None:
        self.client = client
        self.max_atoms = max_atoms
        self.budget_chars = budget_chars
        self.policy = policy

    async def process(self, ctx: TurnContext) -> NodeResult:
        result = await self.client.recall(
            ctx.memory_space_uid,
            ctx.request.user_text,
            max_atoms=self.max_atoms,
            budget_chars=self.budget_chars,
            detail="statement",
            policy=self.policy,
        )
        ctx.memory_block = result.get("context_block") or ""
        hits = result.get("hits") or result.get("atoms") or []
        await ctx.publish(
            "memory_recall",
            {
                "chars_used": result.get("chars_used"),
                "has_block": bool(ctx.memory_block),
                "hit_count": len(hits) if isinstance(hits, list) else 0,
                "context_block": ctx.memory_block,
            },
        )
        return NodeResult.CONTINUE
