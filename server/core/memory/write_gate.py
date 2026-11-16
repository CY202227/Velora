"""Route completed user exchanges to the raw memory inbox."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryWriteDecision:
    write: bool
    reason: str


def decide_memory_write(
    user_text: str, *, is_correction: bool = False, proactive: bool = False,
) -> MemoryWriteDecision:
    """Consolidation, not a language heuristic, selects durable facts."""
    if proactive:
        return MemoryWriteDecision(False, "proactive_delivery")
    if is_correction:
        return MemoryWriteDecision(True, "correction")
    return MemoryWriteDecision(True, "conversation_turn")
