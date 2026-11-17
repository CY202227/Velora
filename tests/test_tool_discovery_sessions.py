import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.computer.tools import register_computer_tools
from server.core.execution.command_session import close_commands
from server.core.mcp.manager import tool_name
from server.core.tools import ToolRegistry, execute_tool
from server.core.tools.types import ToolSpec


def context():
    return TurnContext(TurnRequest("s", "test"), "s")


@pytest.mark.asyncio
async def test_discovery_is_turn_local_and_respects_allowlist():
    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"ok":true}')
    registry.register(ToolSpec("plugin_a", "calendar search", handler=handler, deferred=True))
    registry.register(ToolSpec("plugin_b", "calendar private", handler=handler, deferred=True))
    ctx, other = context(), context()
    for turn in (ctx, other):
        turn.extras["tools"], turn.extras["tool_search_spec"] = registry.plan(["plugin_a"])
    assert [t["function"]["name"] for t in ctx.extras["tools"]] == ["tool_search"]
    assert not json.loads(await execute_tool(registry, "plugin_a", {}, ctx))["ok"]
    result = json.loads(await execute_tool(registry, "tool_search", {"query": "calendar"}, ctx))
    assert [t["function"]["name"] for t in result["tools"]] == ["plugin_a"]
    assert json.loads(await execute_tool(registry, "plugin_a", {}, ctx))["ok"]
    assert not json.loads(await execute_tool(registry, "plugin_a", {}, other))["ok"]
    assert not json.loads(await execute_tool(registry, "plugin_b", {}, ctx))["ok"]


def test_plugin_names_are_stable_bounded_and_collision_resistant():
    assert tool_name("a/b", "search") != tool_name("a_b", "search")
    assert tool_name("one", "search") != tool_name("two", "search")
    assert len(tool_name("x" * 200, "y" * 200)) <= 64


@pytest.mark.asyncio
async def test_discovered_tool_schema_reaches_next_model_round():
    from server.core.nodes.llm import LLMNode
    from server.core.provider.base import ChatResult, ToolCall
    ctx = context()
    ctx.llm_base_url = "http://localhost"
    ctx.messages = [{"role": "user", "content": "use calendar"}]
    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"ok":true}')
    registry.register(ToolSpec("calendar", "calendar", handler=handler, deferred=True))
    ctx.extras["tools"], ctx.extras["tool_search_spec"] = registry.plan()
    node = LLMNode(object(), tools=registry, context_window=32000, max_steps=3)
    calls = []
    async def sample(ctx, provider, messages, *, tools):
        calls.append([t["function"]["name"] for t in tools])
        if len(calls) == 1:
            return ChatResult(tool_calls=[ToolCall("find", "tool_search", '{"query":"calendar"}')])
        if len(calls) == 2:
            return ChatResult(tool_calls=[ToolCall("use", "calendar", '{}')])
        return ChatResult(content="done")
    node._stream_round = sample
    await node.process(ctx)
    assert calls == [["tool_search"], ["tool_search", "calendar"], ["tool_search", "calendar"]]
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_handle_poll_cancel_and_turn_cleanup(monkeypatch):
    from server.core.execution.command_session import append_output
    started = asyncio.Event()
    async def execute(*args, **kwargs):
        assert kwargs["execution_id"] == "job1"
        append_output("stdout", "working")
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("server.core.computer.tools.execute", execute)
    registry = ToolRegistry()
    register_computer_tools(registry)
    ctx = context()
    ctx.extras.update(settings=Settings(_env_file=None, execution_snapshots_enabled=False), execution_id="job1", execution_store=object())
    result = json.loads(await execute_tool(registry, "command_session", {"action": "start", "command": "echo test", "wait_ms": 1}, ctx))
    assert started.is_set() and result["status"] == "running" and result["stdout"] == "working"
    ctx.extras["execution_id"] = "different"
    other = context()
    assert not json.loads(await execute_tool(registry, "command_session", {"action": "cancel", "execution_id": "job1"}, other))["ok"]
    await close_commands(ctx)
    assert ctx.extras["command_sessions"]["job1"]["task"].cancelled()


@pytest.mark.asyncio
async def test_restart_rejects_turn_owned_handles_without_replay():
    from server.core.agent.store import RunStore
    store = object.__new__(RunStore)
    store.admission = None
    store.read = AsyncMock(return_value={"run": {"tool_executions": [{"name": "command_session"}]}})
    assert not (await store.recovery_status("run"))["recoverable"]
    with pytest.raises(ValueError, match="cannot be resumed"):
        await store.claim("run")
