"""Load mcp_server.json, connect servers, register tools into ToolRegistry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.mcp.client import McpClient
from server.core.mcp.config import (
    McpServerConfig,
    ensure_mcp_config_file,
    load_mcp_servers,
    load_mcp_servers_raw,
    parse_server_entry,
    save_mcp_servers_raw,
)
from server.core.tools.registry import ToolRegistry
from server.core.tools.types import ToolSpec

logger = logging.getLogger("velora.mcp")


@dataclass
class McpManager:
    settings: Settings
    clients: dict[str, McpClient] = field(default_factory=dict)
    registered_tool_names: list[str] = field(default_factory=list)
    _registry: ToolRegistry | None = None

    async def start(self, registry: ToolRegistry) -> None:
        self._registry = registry
        if not self.settings.mcp_enabled:
            logger.info("MCP disabled — skip")
            return
        path = ensure_mcp_config_file(self.settings.mcp_config_path)
        try:
            servers = load_mcp_servers(path)
        except Exception as exc:
            logger.warning("failed to load MCP config %s: %s", path, exc)
            return
        active = [s for s in servers if s.active]
        if not active:
            logger.info("MCP: no active servers in %s", path)
            return
        for cfg in active:
            await self._start_one(cfg, registry)

    async def _start_one(self, cfg: McpServerConfig, registry: ToolRegistry) -> None:
        client = McpClient(cfg)
        try:
            await client.connect()
        except Exception as exc:
            logger.warning("MCP server %s skipped: %s", cfg.name, exc)
            self.clients[cfg.name] = client
            return
        self.clients[cfg.name] = client
        for tool in client.tools:
            name = tool["name"]
            if registry.get(name) is not None:
                logger.warning(
                    "MCP tool %r from server %s skipped — name already registered",
                    name,
                    cfg.name,
                )
                continue
            handler = _make_handler(self, cfg.name, name)
            registry.register(
                ToolSpec(
                    name=name,
                    description=tool.get("description") or f"MCP:{cfg.name}/{name}",
                    parameters=tool.get("parameters")
                    or {"type": "object", "properties": {}},
                    handler=handler,
                )
            )
            self.registered_tool_names.append(name)
        logger.info(
            "MCP server %s ready — %s tools",
            cfg.name,
            len(client.tools),
        )

    async def stop(self) -> None:
        if self._registry is not None:
            for name in self.registered_tool_names:
                self._registry.remove(name)
        self.registered_tool_names.clear()
        for client in list(self.clients.values()):
            await client.cleanup()
        self.clients.clear()

    async def reload(self, registry: ToolRegistry | None = None) -> dict[str, Any]:
        reg = registry or self._registry
        if reg is None:
            raise RuntimeError("no tool registry")
        await self.stop()
        await self.start(reg)
        return self.status()

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        client = self.clients.get(server_name)
        if client is None:
            return f'{{"ok": false, "error": "unknown MCP server: {server_name}"}}'
        return await client.call_tool(tool_name, arguments)

    def list_config(self) -> dict[str, Any]:
        raw = load_mcp_servers_raw(self.settings.mcp_config_path)
        servers = raw.get("mcpServers") or {}
        return {
            "config_path": self.settings.mcp_config_path,
            "mcpServers": servers,
        }

    async def save_config(
        self, servers: dict[str, Any], *, reload: bool = True
    ) -> dict[str, Any]:
        # Validate each entry
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                raise ValueError(f"server {name!r} must be an object")
            parse_server_entry(str(name), entry)
        save_mcp_servers_raw(self.settings.mcp_config_path, servers)
        if reload and self._registry is not None:
            return await self.reload(self._registry)
        return self.status()

    async def test_server(self, name: str, entry: dict[str, Any]) -> dict[str, Any]:
        cfg = parse_server_entry(name, entry)
        client = McpClient(cfg)
        try:
            await client.connect()
            tools = [
                {"name": t["name"], "description": t.get("description") or ""}
                for t in client.tools
            ]
            return {"ok": True, "name": name, "tool_count": len(tools), "tools": tools}
        except Exception as exc:
            return {"ok": False, "name": name, "error": str(exc), "tool_count": 0}
        finally:
            await client.cleanup()

    def status(self) -> dict[str, Any]:
        # Merge file config with live clients
        raw = load_mcp_servers_raw(self.settings.mcp_config_path)
        file_servers = raw.get("mcpServers") or {}
        servers: list[dict[str, Any]] = []
        for name, entry in file_servers.items():
            client = self.clients.get(name)
            active = True
            if isinstance(entry, dict):
                active = entry.get("active", True) is not False
            if client is not None:
                servers.append(
                    {
                        "name": name,
                        "ok": client.ok,
                        "active": active,
                        "tool_count": len(client.tools) if client.ok else 0,
                        "error": client.error,
                        "transport": client.config.transport,
                    }
                )
            else:
                transport = "stdio"
                if isinstance(entry, dict) and entry.get("url"):
                    transport = entry.get("transport") or entry.get("type") or "sse"
                servers.append(
                    {
                        "name": name,
                        "ok": False,
                        "active": active,
                        "tool_count": 0,
                        "error": None if not active else "not connected",
                        "transport": transport,
                    }
                )
        return {
            "enabled": self.settings.mcp_enabled,
            "config_path": self.settings.mcp_config_path,
            "servers": servers,
            "tool_count": len(self.registered_tool_names),
        }

    def register_tools_from_client_for_tests(
        self, registry: ToolRegistry, client: McpClient
    ) -> list[str]:
        """Register tools from an already-populated client (unit tests)."""
        self.clients[client.name] = client
        added: list[str] = []
        for tool in client.tools:
            name = tool["name"]
            if registry.get(name) is not None:
                logger.warning(
                    "MCP tool %r from server %s skipped — name already registered",
                    name,
                    client.name,
                )
                continue
            registry.register(
                ToolSpec(
                    name=name,
                    description=tool.get("description") or name,
                    parameters=tool.get("parameters")
                    or {"type": "object", "properties": {}},
                    handler=_make_handler(self, client.name, name),
                )
            )
            self.registered_tool_names.append(name)
            added.append(name)
        self._registry = registry
        return added


def _make_handler(manager: McpManager, server_name: str, tool_name: str):
    async def _handler(args: dict[str, Any], ctx: TurnContext) -> str:
        del ctx
        return await manager.call_tool(server_name, tool_name, args)

    return _handler
