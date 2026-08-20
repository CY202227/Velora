"""Tavily web_search builtin tool."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from server.core.chain.context import TurnContext
from server.core.tools.registry import ToolRegistry
from server.core.tools.types import ToolSpec

logger = logging.getLogger("velora.tools")


async def tavily_search(query: str, *, api_key: str, max_results: int = 5) -> str:
    if not api_key:
        return json.dumps(
            {"ok": False, "error": "tavily_api_key not configured"},
            ensure_ascii=False,
        )
    q = (query or "").strip()
    if not q:
        return json.dumps({"ok": False, "error": "empty query"}, ensure_ascii=False)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": q,
                    "max_results": max(1, min(max_results, 10)),
                    "include_answer": False,
                },
            )
            if resp.status_code >= 400:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"tavily HTTP {resp.status_code}: {resp.text[:500]}",
                    },
                    ensure_ascii=False,
                )
            data = resp.json()
    except Exception as exc:
        logger.warning("tavily search failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    results = []
    for item in data.get("results") or []:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
            }
        )
    return json.dumps(
        {"ok": True, "query": q, "results": results},
        ensure_ascii=False,
    )


def register_web_search_tool(registry: ToolRegistry) -> None:
    async def _web_search(args: dict[str, Any], ctx: TurnContext) -> str:
        s = ctx.extras.get("settings")
        if s is None or not getattr(s, "web_search_enabled", True):
            return json.dumps(
                {"ok": False, "error": "web_search disabled"}, ensure_ascii=False
            )
        return await tavily_search(
            str(args.get("query") or ""),
            api_key=getattr(s, "tavily_api_key", "") or "",
        )

    registry.register(
        ToolSpec(
            name="web_search",
            description="使用 Tavily 搜索网页，返回标题/URL/摘要。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_web_search,
        )
    )
