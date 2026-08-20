"""MCP config parse, registry merge, name collision, disable."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.mcp.client import McpClient
from server.core.mcp.config import (
    ensure_mcp_config_file,
    load_mcp_servers,
    normalize_input_schema,
    parse_server_entry,
)
from server.core.mcp.manager import McpManager
from server.core.tools import ToolRegistry, execute_tool, register_builtin_tools


def test_parse_stdio_and_http() -> None:
    stdio = parse_server_entry(
        "demo",
        {"command": "npx", "args": ["-y", "pkg"], "active": True},
    )
    assert stdio.transport == "stdio"
    assert stdio.command == "npx"
    assert stdio.args == ["-y", "pkg"]
    assert stdio.connect_kwargs()["command"] == "npx"

    sse = parse_server_entry(
        "remote",
        {"url": "http://127.0.0.1:9/sse", "transport": "sse"},
    )
    assert sse.transport == "sse"
    assert sse.url.endswith("/sse")

    http = parse_server_entry(
        "http",
        {"url": "http://127.0.0.1:9/mcp", "type": "streamable_http"},
    )
    assert http.transport == "streamable_http"


def test_normalize_input_schema() -> None:
    assert normalize_input_schema(None)["type"] == "object"
    assert "properties" in normalize_input_schema({"type": "object"})


def test_load_mcp_servers(tmp_path: Path) -> None:
    p = tmp_path / "mcp_server.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {"command": "uvx", "args": ["demo"], "active": False},
                    "b": {
                        "url": "http://x/mcp",
                        "transport": "streamable_http",
                        "active": True,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    servers = load_mcp_servers(p)
    assert len(servers) == 2
    assert servers[0].active is False
    assert servers[1].active is True
    assert servers[1].transport == "streamable_http"


def test_ensure_mcp_config_file(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "mcp_server.json"
    out = ensure_mcp_config_file(p)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {}}


@pytest.mark.asyncio
async def test_mcp_disabled_noop(tmp_path: Path) -> None:
    settings = Settings(
        mcp_enabled=False,
        mcp_config_path=(tmp_path / "mcp_server.json").as_posix(),
    )
    reg = ToolRegistry()
    register_builtin_tools(reg)
    mgr = McpManager(settings)
    await mgr.start(reg)
    assert len(reg.names()) == 4
    assert mgr.status()["enabled"] is False
    assert mgr.status()["tool_count"] == 0
    await mgr.stop()


@pytest.mark.asyncio
async def test_register_mcp_tools_and_execute() -> None:
    settings = Settings(mcp_enabled=True)
    reg = ToolRegistry()
    register_builtin_tools(reg)
    mgr = McpManager(settings)

    client = McpClient(
        parse_server_entry("fake", {"command": "true", "active": True})
    )
    client.ok = True
    client.tools = [
        {
            "name": "echo_ping",
            "description": "ping",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    client.call_tool = AsyncMock(return_value='{"pong": true}')  # type: ignore[method-assign]

    added = mgr.register_tools_from_client_for_tests(reg, client)
    assert added == ["echo_ping"]
    assert reg.get("echo_ping") is not None
    assert len(reg.names()) == 5

    ctx = TurnContext(
        request=TurnRequest(session_id="s", user_text="x"),
        session_id="s",
    )
    out = await execute_tool(reg, "echo_ping", "{}", ctx)
    assert "pong" in out
    client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_name_collision_skips_mcp() -> None:
    settings = Settings(mcp_enabled=True)
    reg = ToolRegistry()
    register_builtin_tools(reg)
    mgr = McpManager(settings)
    client = McpClient(
        parse_server_entry("fake", {"command": "true", "active": True})
    )
    client.ok = True
    client.tools = [
        {
            "name": "get_current_time",
            "description": "collide",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    added = mgr.register_tools_from_client_for_tests(reg, client)
    assert added == []
    # builtin still wins
    spec = reg.get("get_current_time")
    assert spec is not None
    assert "时区" in (spec.description or "") or "时间" in (spec.description or "")


@pytest.mark.asyncio
async def test_empty_config_active_none(tmp_path: Path) -> None:
    p = tmp_path / "mcp_server.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {"command": "npx", "args": ["x"], "active": False}
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(mcp_enabled=True, mcp_config_path=p.as_posix())
    reg = ToolRegistry()
    register_builtin_tools(reg)
    mgr = McpManager(settings)
    await mgr.start(reg)
    assert len(reg.names()) == 4
    await mgr.stop()
