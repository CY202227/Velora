import pytest

from server.core.memory.write_gate import decide_memory_write


@pytest.mark.parametrize("text", [
    "你好", "I watched The Glass Menagerie yesterday.", "昨日、演劇を観ました。",
    "帮我算一下 17 × 4", "",
])
def test_completed_turns_reach_memory_without_language_filter(text: str) -> None:
    assert decide_memory_write(text).write


def test_memory_gate_keeps_explicit_correction() -> None:
    decision = decide_memory_write("其实我不喝咖啡了", is_correction=True)
    assert decision.write and decision.reason == "correction"


def test_memory_gate_skips_proactive_delivery() -> None:
    assert not decide_memory_write("会议开始", proactive=True).write
