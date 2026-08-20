"""Agent local 1C: workspace, skills, web_search, MCP reload."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.computer.policy import check_shell_command
from server.core.computer.workspace import WorkspaceError, resolve_in_workspace
from server.core.mcp.config import save_mcp_servers_raw
from server.core.mcp.client import McpClient
from server.core.mcp.config import parse_server_entry
from server.core.mcp.manager import McpManager
from server.core.nodes.compose_prompt import ComposePromptNode
from server.core.persona.default import DEFAULT_PERSONA
from server.core.skills.manager import SkillManager
from server.core.tools import ToolRegistry, register_builtin_tools
from server.core.tools.web_search import tavily_search
from server.app_state import sync_optional_tools


def test_workspace_escape_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "workspaces"
    root = resolve_in_workspace(ws, "sess-1", "ok.txt")
    assert root.name == "ok.txt"
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(ws, "sess-1", "../outside.txt")
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(ws, "sess-1", str(tmp_path / "abs.txt"))


def test_workspace_skills_extra_root(tmp_path: Path) -> None:
    ws = tmp_path / "workspaces"
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = skills / "hello" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# hi", encoding="utf-8")
    resolved = resolve_in_workspace(
        ws,
        "s1",
        str(skill_md.resolve()),
        extra_roots=[skills],
    )
    assert resolved == skill_md.resolve()


def test_shell_policy_blocks_danger() -> None:
    assert check_shell_command("echo hi") is None
    assert check_shell_command("rm -rf /") is not None
    assert check_shell_command("format C:") is not None
    assert check_shell_command("powershell -enc QQBh") is not None


def test_skills_discover(tmp_path: Path) -> None:
    root = tmp_path / "skills" / "hello"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: hello\ndescription: demo skill\n---\n\n# Hello\n",
        encoding="utf-8",
    )
    mgr = SkillManager(tmp_path / "skills")
    items = mgr.list_skills()
    assert len(items) == 1
    assert items[0].name == "hello"
    prompt = mgr.build_skills_prompt()
    assert "hello" in prompt
    assert "demo skill" in prompt
    assert mgr.list_skills([]) == []


@pytest.mark.asyncio
async def test_skills_in_compose_prompt(tmp_path: Path) -> None:
    root = tmp_path / "skills" / "pack"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: pack\ndescription: pack desc\n---\nbody\n",
        encoding="utf-8",
    )
    mgr = SkillManager(tmp_path / "skills")
    ctx = TurnContext(
        request=TurnRequest(session_id="s", user_text="x"),
        session_id="s",
    )
    ctx.persona = DEFAULT_PERSONA
    ctx.history = []
    ctx.memory_block = ""
    ctx.style_knobs = {"warmth": 35}
    await ComposePromptNode(skills=mgr).process(ctx)
    sys_msg = ctx.messages[0]["content"] or ""
    assert "pack" in sys_msg
    assert "pack desc" in sys_msg


@pytest.mark.asyncio
async def test_web_search_tavily_mock() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"title": "A", "url": "https://a.test", "content": "snippet"},
        ]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("server.core.tools.web_search.httpx.AsyncClient", return_value=mock_client):
        out = await tavily_search("velora", api_key="tvly-test")
    data = json.loads(out)
    assert data["ok"] is True
    assert data["results"][0]["title"] == "A"

    no_key = json.loads(await tavily_search("q", api_key=""))
    assert no_key["ok"] is False


@pytest.mark.asyncio
async def test_sync_optional_tools_toggle() -> None:
    settings = Settings(computer_enabled=True, web_search_enabled=True)
    reg = ToolRegistry()
    register_builtin_tools(reg)
    sync_optional_tools(reg, settings)
    assert reg.get("run_shell") is not None
    assert reg.get("web_search") is not None
    settings.computer_enabled = False
    settings.web_search_enabled = False
    sync_optional_tools(reg, settings)
    assert reg.get("run_shell") is None
    assert reg.get("web_search") is None
    assert reg.get("get_current_time") is not None


@pytest.mark.asyncio
async def test_mcp_save_reload_preserves_builtins(tmp_path: Path) -> None:
    cfg_path = tmp_path / "mcp_server.json"
    save_mcp_servers_raw(cfg_path, {})
    settings = Settings(
        mcp_enabled=True,
        mcp_config_path=cfg_path.as_posix(),
        computer_enabled=True,
        web_search_enabled=True,
    )
    reg = ToolRegistry()
    register_builtin_tools(reg)
    sync_optional_tools(reg, settings)
    assert reg.get("run_shell") is not None
    mgr = McpManager(settings)
    await mgr.start(reg)

    client = McpClient(
        parse_server_entry("fake", {"command": "true", "active": True})
    )
    client.ok = True
    client.tools = [
        {
            "name": "mcp_only_tool",
            "description": "x",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "get_current_time",
            "description": "collide",
            "parameters": {"type": "object", "properties": {}},
        },
    ]
    mgr.register_tools_from_client_for_tests(reg, client)
    assert reg.get("mcp_only_tool") is not None
    assert "时区" in (reg.get("get_current_time").description or "") or "时间" in (
        reg.get("get_current_time").description or ""
    )

    save_mcp_servers_raw(
        cfg_path,
        {"gone": {"command": "npx", "args": ["x"], "active": False}},
    )
    # reload should drop MCP tools, keep computer/builtin
    await mgr.reload(reg)
    assert reg.get("mcp_only_tool") is None
    assert reg.get("run_shell") is not None
    assert reg.get("get_current_time") is not None
    assert reg.get("web_search") is not None
    await mgr.stop()


@pytest.mark.asyncio
async def test_mcp_put_config_via_save(tmp_path: Path) -> None:
    cfg_path = tmp_path / "mcp_server.json"
    settings = Settings(mcp_enabled=True, mcp_config_path=cfg_path.as_posix())
    reg = ToolRegistry()
    register_builtin_tools(reg)
    mgr = McpManager(settings)
    await mgr.start(reg)
    status = await mgr.save_config(
        {"demo": {"command": "npx", "args": ["-y", "x"], "active": False}},
        reload=True,
    )
    assert cfg_path.is_file()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "demo" in raw["mcpServers"]
    assert status["enabled"] is True
    # inactive → no extra tools
    assert reg.get("get_current_time") is not None
    await mgr.stop()
