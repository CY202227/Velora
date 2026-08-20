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
