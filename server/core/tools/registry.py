"""Named tool registry."""

from __future__ import annotations

from typing import Any

from server.core.tools.types import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name required")
        if spec.name == "tool_search":
            raise ValueError("tool_search is reserved for per-turn discovery")
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def filter(self, names: list[str] | None) -> list[ToolSpec]:
        """None = all; [] = none; list = subset (unknown names skipped)."""
        if names is None:
            return list(self._tools.values())
        return [self._tools[n] for n in names if n in self._tools]

    def openai_tools(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        return [s.openai_schema() for s in self.filter(names)]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def plan(self, names=None):
        """Keep deferred plugin schemas out of initial requests."""
        import json
        import re
        candidates = self.filter(names)
        deferred = [s for s in candidates if s.deferred]
        direct = [s.openai_schema() for s in candidates if not s.deferred]
        if not deferred:
            return direct, None

        async def search(args, ctx):
            terms = set(re.findall(r"\w+", args["query"].lower()))
            ranked = sorted(deferred, key=lambda s: (-sum(t in (s.name + " " + s.description).lower() for t in terms), s.name))
            found = [s for s in ranked if any(t in (s.name + " " + s.description).lower() for t in terms)][:args.get("limit", 5)]
            payload = ctx.extras["tools"]
            enabled = {t["function"]["name"] for t in payload}
            for spec in found:
                if spec.name not in enabled:
                    payload.append(spec.openai_schema())
            return json.dumps({"tools": [s.openai_schema() for s in found]}, ensure_ascii=False)

        # The handler is per-turn; never put its allowed candidates in shared state.
        schema = {"type": "object", "properties": {"query": {"type": "string", "minLength": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"], "additionalProperties": False}
        direct.append(ToolSpec("tool_search", "搜索当前允许的插件工具并开放其调用；可用工具名称和描述中的关键词。", schema, search).openai_schema())
        return direct, ToolSpec("tool_search", "搜索插件工具", schema, search)
