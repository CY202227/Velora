from types import SimpleNamespace

from server.core.conversation.compaction import compact_history


def test_compaction_keeps_order_and_stays_bounded() -> None:
    turns = [
        SimpleNamespace(role="user", content="第一件事"),
        SimpleNamespace(role="assistant", content="我记下了"),
        SimpleNamespace(role="user", content="这是很长的补充说明" * 20),
    ]
    result = compact_history(turns, max_chars=48)
    assert result.startswith("用户：第一件事\n助手：我记下了\n用户：")
    assert result.endswith("…")
    assert len(result) <= 48


def test_compaction_ignores_empty_or_unknown_roles() -> None:
    turns = [
        SimpleNamespace(role="tool", content="secret"),
        SimpleNamespace(role="user", content="  hello\nworld  "),
        SimpleNamespace(role="assistant", content=""),
    ]
    assert compact_history(turns, max_chars=100) == "用户：hello world"
