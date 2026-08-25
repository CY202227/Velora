"""Execute a named tool and return a string result for the model."""

from __future__ import annotations

import json
import logging
from typing import Any

from server.core.chain.context import TurnContext
from server.core.tools.registry import ToolRegistry

logger = logging.getLogger("velora.tools")


async def execute_tool(
    registry: ToolRegistry,
    name: str,
    arguments: str | dict[str, Any],
    ctx: TurnContext,
) -> str:
    spec = registry.get(name)
    if spec is None or spec.handler is None:
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "error": f"invalid JSON arguments: {arguments[:200]}"}
            )
    else:
        args = arguments or {}
    if not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "arguments must be an object"})
    try:
        return await spec.handler(args, ctx)
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return json.dumps({"ok": False, "error": str(exc)})
