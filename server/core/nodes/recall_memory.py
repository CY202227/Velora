from __future__ import annotations

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.memory.client import AtomMemoryClient


def _explainable_hits(raw_hits: object) -> list[dict[str, object]]:
    """Return the small, user-safe portion of a recall result for the Desk.

    atom-memory owns the complete recall contract.  The conversation stream
    should expose only the statement the model was given plus its stable key,
    never the internal prompt block or source payloads.
    """
    if not isinstance(raw_hits, list):
        return []
    items: list[dict[str, object]] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        key = hit.get("key")
        statement = hit.get("statement")
        if not isinstance(key, str) or not isinstance(statement, str):
            continue
        items.append(
            {
                "key": key,
                "statement": statement,
                "kind": hit.get("kind") if isinstance(hit.get("kind"), str) else "memory",
                "memory_layer": hit.get("memory_layer"),
            }
        )
    return items


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
        # A direct "please remember / correct" turn is the user overriding
        # memory. Do not inject possibly stale facts into the model before the
        # correction is persisted and consolidated.
        if ctx.is_correction:
            ctx.memory_block = ""
            await ctx.publish(
                "memory_recall",
                {
                    "chars_used": 0,
                    "has_block": False,
                    "hit_count": 0,
                    "items": [],
                    "skipped": "correction",
                },
            )
            return NodeResult.SKIP

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
        explainable_hits = _explainable_hits(hits)
        await ctx.publish(
            "memory_recall",
            {
                "chars_used": result.get("chars_used"),
                "has_block": bool(ctx.memory_block),
                "hit_count": len(explainable_hits),
                "items": explainable_hits,
            },
        )
        return NodeResult.CONTINUE
