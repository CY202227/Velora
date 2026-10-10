"""Parse the local MCP server JSON configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

TransportKind = Literal["stdio", "sse", "streamable_http"]


@dataclass
class McpServerConfig:
    name: str
    active: bool
    transport: TransportKind
    # stdio
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    # http
    url: str | None = None
    headers: dict[str, str] | None = None
    timeout: float = 30.0
    sse_read_timeout: float = 300.0
    session_read_timeout: float = 60.0
    terminate_on_close: bool = True

    def connect_kwargs(self) -> dict[str, Any]:
        """Raw dict for MCPClient.connect (without name/active)."""
        if self.transport == "stdio":
            out: dict[str, Any] = {"command": self.command or ""}
            if self.args is not None:
                out["args"] = self.args
            if self.env is not None:
                out["env"] = self.env
            return out
        return {
            "url": self.url,
            "transport": self.transport,
            "headers": self.headers or {},
            "timeout": self.timeout,
            "sse_read_timeout": self.sse_read_timeout,
            "session_read_timeout": self.session_read_timeout,
            "terminate_on_close": self.terminate_on_close,
        }


def normalize_input_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    out = dict(schema)
    if out.get("type") is None:
        out["type"] = "object"
    if "properties" not in out or not isinstance(out.get("properties"), dict):
        out["properties"] = out.get("properties") if isinstance(out.get("properties"), dict) else {}
    return out


def parse_server_entry(name: str, raw: dict[str, Any]) -> McpServerConfig:
    active = raw.get("active", True) is not False
    if "url" in raw and raw["url"]:
        transport_raw = raw.get("transport") or raw.get("type") or "sse"
        if transport_raw == "streamable_http":
            kind: TransportKind = "streamable_http"
        else:
            kind = "sse"
        return McpServerConfig(
            name=name,
            active=active,
            transport=kind,
            url=str(raw["url"]),
            headers=dict(raw.get("headers") or {}) if isinstance(raw.get("headers"), dict) else {},
            timeout=float(raw.get("timeout", 30)),
            sse_read_timeout=float(raw.get("sse_read_timeout", 300)),
            session_read_timeout=float(raw.get("session_read_timeout", 60)),
            terminate_on_close=bool(raw.get("terminate_on_close", True)),
        )
    command = raw.get("command")
    if not command:
        raise ValueError(f"MCP server {name!r}: need url or command")
    args = raw.get("args")
    env = raw.get("env")
    return McpServerConfig(
        name=name,
        active=active,
        transport="stdio",
        command=str(command),
        args=list(args) if isinstance(args, list) else None,
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else None,
    )


def load_mcp_servers(path: str | Path) -> list[McpServerConfig]:
    p = Path(path)
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    out: list[McpServerConfig] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        out.append(parse_server_entry(str(name), raw))
    return out


def ensure_mcp_config_file(path: str | Path) -> Path:
    """Create empty shell if missing. Returns path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file():
        p.write_text(
            json.dumps({"mcpServers": {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return p


def load_mcp_servers_raw(path: str | Path) -> dict[str, Any]:
    p = ensure_mcp_config_file(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"mcpServers": {}}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        data["mcpServers"] = {}
    return data


def save_mcp_servers_raw(path: str | Path, servers: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mcpServers": servers}
    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
