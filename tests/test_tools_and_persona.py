"""Function calling skeleton + thickened persona."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult, TurnRequest
from server.core.conversation.store import ConversationStore
from server.core.nodes.compose_prompt import ComposePromptNode
from server.core.nodes.llm import LLMNode
from server.core.persona.default import DEFAULT_PERSONA, Persona, get_persona
from server.core.provider.base import ChatResult, ToolCall
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.tools import ToolRegistry, execute_tool, register_builtin_tools


@pytest.fixture
async def store(tmp_path: Path) -> ConversationStore:
    db = (tmp_path / "t.db").as_posix()
    s = ConversationStore(f"sqlite+aiosqlite:///{db}")
    await s.init()
    yield s
    await s.close()


def test_persona_begin_dialogs_and_tools() -> None:
    p = get_persona()
    assert p.id == DEFAULT_PERSONA.id
    assert len(p.begin_dialogs) >= 2
    assert len(p.begin_dialogs) % 2 == 0
    assert p.tool_names is None
    assert p.opener


@pytest.mark.asyncio
async def test_provider_chat_parses_tool_calls() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_current_time",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = payload
    mock_resp.text = json.dumps(payload)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("server.core.provider.openai_compat.httpx.AsyncClient", return_value=mock_client):
        provider = OpenAICompatProvider("http://llm.test/v1", "k")
        result = await provider.chat(
            [{"role": "user", "content": "几点了"}],
            model="m",
            tools=[{"type": "function", "function": {"name": "get_current_time"}}],
        )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_current_time"
    assert result.content is None
    body = mock_client.post.await_args.kwargs["json"]
    assert "tools" in body


@pytest.mark.asyncio
async def test_get_current_time_timezone() -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    ctx = TurnContext(
        request=TurnRequest(session_id="s", user_text="x"),
        session_id="s",
    )
    ctx.extras["settings"] = Settings(user_timezone="Asia/Shanghai")
    out = await execute_tool(reg, "get_current_time", "{}", ctx)
    data = json.loads(out)
    assert "local" in data
    assert "Asia/Shanghai" in data["timezone"] or data["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_create_reminder_tool_quiet_defer(store: ConversationStore) -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    ctx = TurnContext(
        request=TurnRequest(session_id=sess.id, user_text="x"),
        session_id=sess.id,
    )
    # due at 23:00 Shanghai = 15:00 UTC → quiet 22-08 → defer to 08:00 next day UTC midnight
    ctx.extras["store"] = store
    ctx.extras["settings"] = Settings(
        user_timezone="Asia/Shanghai",
        quiet_hours_enabled=True,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
    )
    out = await execute_tool(
        reg,
        "create_reminder",
        {
            "note": "睡觉前勿扰",
            "due_at": "2026-08-05T23:00:00+08:00",
        },
        ctx,
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["due_at"].startswith("2026-08-06T00:00:00")


@pytest.mark.asyncio
async def test_create_reminder_tool_cron(store: ConversationStore) -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    ctx = TurnContext(
        request=TurnRequest(session_id=sess.id, user_text="x"),
        session_id=sess.id,
    )
    ctx.extras["store"] = store
    ctx.extras["settings"] = Settings(
        user_timezone="UTC",
        quiet_hours_enabled=False,
    )
    out = await execute_tool(
        reg,
        "create_reminder",
        {"note": "站会", "cron_expression": "0 10 * * 1-5"},
        ctx,
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["schedule_kind"] == "cron"
    assert data["cron_expr"] == "0 10 * * 1-5"


@pytest.mark.asyncio
async def test_compose_begin_dialogs_and_tool_filter() -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    persona = Persona(
        id="t",
        name="t",
        system_prompt="sys",
        begin_dialogs=["hi", "hello there"],
        tool_names=[],
    )
    ctx = TurnContext(
        request=TurnRequest(session_id="s", user_text="现在几点"),
        session_id="s",
        persona=persona,
        history=[],
        style_knobs={"warmth": 35},
    )
    node = ComposePromptNode(reg)
    await node.process(ctx)
    roles = [m["role"] for m in ctx.messages]
    assert roles[:4] == ["system", "user", "assistant", "user"]
    assert ctx.messages[1]["content"] == "hi"
    assert ctx.messages[2]["content"] == "hello there"
    assert ctx.extras["tools"] == []

    persona.tool_names = None
    ctx2 = TurnContext(
        request=TurnRequest(session_id="s", user_text="x"),
        session_id="s",
        persona=persona,
        style_knobs={"warmth": 35},
    )
    await ComposePromptNode(reg).process(ctx2)
    assert len(ctx2.extras["tools"]) == 4


@pytest.mark.asyncio
async def test_proactive_compose_strips_reminder_tools() -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    persona = Persona(
        id="t",
        name="t",
        system_prompt="sys",
        begin_dialogs=[],
        tool_names=None,
    )
    ctx = TurnContext(
        request=TurnRequest(
            session_id="s",
            user_text="请按系统说明完成到期提醒，并直接通知用户。",
            client_meta={"proactive": True, "reminder_note": "去上厕所"},
        ),
        session_id="s",
        persona=persona,
        style_knobs={"warmth": 35},
        extras={"proactive": True},
    )
    await ComposePromptNode(reg).process(ctx)
    names = {
        (t.get("function") or {}).get("name") for t in ctx.extras["tools"]
    }
    assert "create_reminder" not in names
    assert "list_reminders" not in names
    assert "到期提醒" in (ctx.messages[0].get("content") or "")
    assert "去上厕所" in (ctx.messages[0].get("content") or "")


@pytest.mark.asyncio
async def test_llm_tool_loop_final_text(store: ConversationStore) -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )

    class FakeProvider:
        def __init__(self) -> None:
            self.n = 0
            self.stream_chat_calls = 0

        async def stream_chat(self, messages, *, model, tools=None, tool_choice=None):
            from server.core.provider.base import StreamEvent

            del messages, model, tool_choice
            self.stream_chat_calls += 1
            self.n += 1
            if self.n == 1:
                assert tools  # first round should carry tools
                # Some providers emit a sentence before the final chunk proves
                # this is a tool call. The client must discard that draft.
                yield StreamEvent(delta="我先查一下。")
                yield StreamEvent(
                    final=ChatResult(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="c1", name="get_current_time", arguments="{}"
                            )
                        ],
                    )
                )
                return
            # Second round: final answer in the same stream (no extra generate)
            assert tools is None or tools is not None
            yield StreamEvent(delta="现在是")
            yield StreamEvent(delta="测试时间。")
            yield StreamEvent(final=ChatResult(content="现在是测试时间。", tool_calls=[]))

        async def chat(self, messages, *, model, tools=None, tool_choice=None):
            raise AssertionError("chat() must not be used when stream_chat exists")

        async def stream(self, messages, *, model):
            raise AssertionError("stream() must not be used when stream_chat exists")

    provider = FakeProvider()
    ctx = TurnContext(
        request=TurnRequest(session_id=sess.id, user_text="几点了"),
        session_id=sess.id,
        model="m",
        llm_base_url="http://127.0.0.1",
        llm_api_key="k",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "几点了"},
        ],
    )
    ctx.extras["tools"] = reg.openai_tools()
    ctx.extras["settings"] = Settings(user_timezone="UTC")
    ctx.extras["store"] = store
    events: list[tuple[str, dict]] = []

    async def emit(ev):
        events.append((ev.type, ev.data))

    ctx.emit = emit

    node = LLMNode(provider, tools=reg, max_tool_rounds=4)  # type: ignore[arg-type]
    result = await node.process(ctx)
    assert result is NodeResult.CONTINUE
    assert ctx.assistant_text == "现在是测试时间。"
    event_names = [name for name, _ in events]
    assert "tool_call" in event_names
    assert "tool_result" in event_names
    assert "token" in event_names
    reset_at = event_names.index("assistant_draft_reset")
    tool_at = event_names.index("tool_call")
    assert reset_at < tool_at
    assert events[reset_at][1]["reason"] == "tool_call"
    assert provider.stream_chat_calls == 2  # tool round + final; never a probe+replay

    await store.add_turn(sess.id, "user", "几点了")
    await store.add_turn(sess.id, "assistant", ctx.assistant_text)
    turns = await store.list_turns(sess.id)
    assert all(t.role in ("user", "assistant") for t in turns)


@pytest.mark.asyncio
async def test_llm_plain_reply_single_stream_with_tools() -> None:
    """Idle chat with tools attached must not double-generate."""
    from server.core.provider.base import StreamEvent

    reg = ToolRegistry()
    register_builtin_tools(reg)

    class FakeProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, *, model, tools=None, tool_choice=None):
            del messages, model, tool_choice
            self.calls += 1
            assert tools  # tools still offered
            yield StreamEvent(delta="你好呀")
            yield StreamEvent(final=ChatResult(content="你好呀", tool_calls=[]))

    provider = FakeProvider()
    ctx = TurnContext(
        request=TurnRequest(session_id="s", user_text="你好"),
        session_id="s",
        model="m",
        llm_base_url="http://127.0.0.1",
        llm_api_key="k",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "你好"},
        ],
    )
    ctx.extras["tools"] = reg.openai_tools()
    tokens: list[str] = []

    async def emit(ev):
        if ev.type == "token":
            tokens.append(ev.data.get("text") or "")

    ctx.emit = emit
    node = LLMNode(provider, tools=reg, max_tool_rounds=4)  # type: ignore[arg-type]
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert ctx.assistant_text == "你好呀"
    assert provider.calls == 1
    assert "".join(tokens) == "你好呀"


def test_merge_tool_call_delta() -> None:
    from server.core.provider.openai_compat import (
        _merge_tool_call_delta,
        _parse_tool_calls,
    )

    acc: dict[int, dict] = {}
    _merge_tool_call_delta(
        acc,
        [
            {
                "index": 0,
                "id": "c1",
                "type": "function",
                "function": {"name": "get_current_time", "arguments": ""},
            }
        ],
    )
    _merge_tool_call_delta(
        acc, [{"index": 0, "function": {"arguments": "{\"x\":"}}]
    )
    _merge_tool_call_delta(
        acc, [{"index": 0, "function": {"arguments": "1}"}}]
    )
    calls = _parse_tool_calls([acc[i] for i in sorted(acc)])
    assert len(calls) == 1
    assert calls[0].name == "get_current_time"
    assert calls[0].arguments == '{"x":1}'
