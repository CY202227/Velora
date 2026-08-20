"""Single MCP server connection (stdio / sse / streamable_http)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from server.core.mcp.config import McpServerConfig, normalize_input_schema

logger = logging.getLogger("velora.mcp")


class McpClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.name = config.name
        self.session: Any = None
        self.tools: list[dict[str, Any]] = []  # {name, description, parameters}
        self.error: str | None = None
        self.ok = False
        self._stack: AsyncExitStack | None = None
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None

    async def connect(self) -> None:
        if self._task and not self._task.done():
            await self.cleanup()
        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        self._ready = ready
        self._task = asyncio.create_task(
            self._run_connection(ready), name=f"mcp:{self.name}"
        )
        try:
            await ready
        except Exception as exc:
            self.error = str(exc)
            self.ok = False
            raise

    async def _run_connection(self, ready: asyncio.Future) -> None:
        stack = AsyncExitStack()
        self._stack = stack
        try:
            await stack.__aenter__()
            await self._do_connect(stack)
            tools = await self._list_tools()
            self.tools = tools
            self.ok = True
            self.error = None
            if not ready.done():
                ready.set_result(None)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(asyncio.CancelledError())
            raise
        except Exception as exc:
            self.ok = False
            self.error = str(exc)
            if not ready.done():
                ready.set_exception(exc)
            logger.warning("MCP server %s connect failed: %s", self.name, exc)
        finally:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.debug("MCP %s stack close: %s", self.name, exc)
            self.session = None
            self._stack = None

    async def _do_connect(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cfg = self.config
        if cfg.transport == "stdio":
            params = StdioServerParameters(
                command=cfg.command or "",
                args=cfg.args or [],
                env=cfg.env,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            self.session = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await self.session.initialize()
            return

        url = cfg.url or ""
        headers = cfg.headers or {}
        if cfg.transport == "streamable_http":
            import httpx
            from mcp.client.streamable_http import streamable_http_client

            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(cfg.timeout, read=cfg.sse_read_timeout),
                    follow_redirects=True,
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client(
                    url=url,
                    http_client=http_client,
                    terminate_on_close=cfg.terminate_on_close,
                )
            )
            read, write = streams[0], streams[1]
            self.session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=cfg.session_read_timeout),
                )
            )
            await self.session.initialize()
            return

        # sse
        from mcp.client.sse import sse_client

        streams = await stack.enter_async_context(
            sse_client(
                url=url,
                headers=headers,
                timeout=cfg.timeout,
                sse_read_timeout=cfg.sse_read_timeout,
            )
        )
        self.session = await stack.enter_async_context(
            ClientSession(
                *streams,
                read_timeout_seconds=timedelta(seconds=cfg.session_read_timeout),
            )
        )
        await self.session.initialize()

    async def _list_tools(self) -> list[dict[str, Any]]:
        assert self.session is not None
        result = await self.session.list_tools()
        tools_out: list[dict[str, Any]] = []
        for t in result.tools or []:
            schema = normalize_input_schema(getattr(t, "inputSchema", None) or {})
            tools_out.append(
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": schema,
                }
            )
        return tools_out

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                if self.session is None:
                    await self.connect()
                assert self.session is not None
                result = await self.session.call_tool(tool_name, arguments=arguments)
                return _format_call_result(result)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "MCP %s call %s failed (attempt %s): %s",
                    self.name,
                    tool_name,
                    attempt + 1,
                    exc,
                )
                if attempt >= 2:
                    break
                try:
                    await self.cleanup()
                    await self.connect()
                except Exception as reconnect_exc:
                    last_exc = reconnect_exc
        return json.dumps(
            {"ok": False, "error": str(last_exc or "mcp call failed")},
            ensure_ascii=False,
        )

    async def cleanup(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.session = None
        self.ok = False


def _format_call_result(result: Any) -> str:
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        typ = getattr(block, "type", None)
        if typ == "text" or hasattr(block, "text"):
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        else:
            try:
                parts.append(json.dumps(block.model_dump(), ensure_ascii=False))
            except Exception:
                parts.append(str(block))
    if getattr(result, "isError", False) and not parts:
        return json.dumps({"ok": False, "error": "mcp tool error"}, ensure_ascii=False)
    if len(parts) == 1:
        return parts[0]
    if parts:
        return "\n".join(parts)
    return json.dumps({"ok": True, "result": str(result)}, ensure_ascii=False)
