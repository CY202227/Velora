from server.core.memory.write_gate import decide_memory_write


def test_memory_gate_keeps_durable_personal_facts() -> None:
    decision = decide_memory_write("以后请叫我小周，我喜欢手冲咖啡。")
    assert decision.write is True
    assert decision.reason == "identity"


def test_memory_gate_skips_ephemeral_chat() -> None:
    assert decide_memory_write("你好").write is False
    assert decide_memory_write("帮我算一下 17 × 4").reason == "no_durable_signal"


def test_memory_gate_prioritizes_explicit_correction() -> None:
    decision = decide_memory_write("其实我不喝咖啡了", is_correction=True)
    assert decision.write is True
    assert decision.reason == "correction"


def test_memory_gate_skips_proactive_delivery() -> None:
    decision = decide_memory_write("会议开始", proactive=True)
    assert decision.write is False
    assert decision.reason == "proactive_delivery"
