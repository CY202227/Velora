"""MCP status + CRUD + reload."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class ServersBody(BaseModel):
    mcpServers: dict[str, Any] = Field(default_factory=dict)


class TestBody(BaseModel):
    name: str
    config: dict[str, Any]


@router.get("/status")
async def mcp_status(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    mcp = getattr(state, "mcp", None)
    if mcp is None:
        return {
            "enabled": False,
            "config_path": "",
            "servers": [],
            "tool_count": 0,
        }
    return mcp.status()


@router.get("/servers")
async def mcp_servers(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    return state.mcp.list_config()


@router.put("/servers")
async def put_mcp_servers(body: ServersBody, request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    try:
        return await state.mcp.save_config(body.mcpServers, reload=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/reload")
async def reload_mcp(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    return await state.mcp.reload(state.tools)


@router.post("/servers/test")
async def test_mcp_server(body: TestBody, request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    try:
        return await state.mcp.test_server(body.name, body.config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
