"""Execute a named tool and return a string result for the model."""

from __future__ import annotations

import json
import logging
from typing import Any
from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

from server.core.chain.context import TurnContext
from server.core.tools.registry import ToolRegistry

logger = logging.getLogger("velora.tools")


async def execute_tool(
    registry: ToolRegistry,
    name: str,
    arguments: str | dict[str, Any],
    ctx: TurnContext,
) -> str:
    spec = ctx.extras.get("tool_search_spec") if name == "tool_search" else registry.get(name)
    if spec is None or spec.handler is None:
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
    if "tools" in ctx.extras:
        allowed = {(item.get("function") or {}).get("name") for item in ctx.extras["tools"] if isinstance(item, dict)}
        if name not in allowed:
            return json.dumps({"ok": False, "error": f"tool is not enabled for this turn: {name}"})
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "error": f"invalid JSON arguments: {arguments[:200]}"}
            )
    else:
        args = {} if arguments is None else arguments
    if not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "arguments must be an object"})
    try:
        validator = validators.validator_for(spec.parameters)
        validator.check_schema(spec.parameters)
        validator(spec.parameters).validate(args)
    except ValidationError as exc:
        return json.dumps({"ok": False, "reason": "invalid_tool_arguments",
                           "error": exc.message})
    except SchemaError:
        logger.error("invalid parameter schema for tool %s", name)
        return json.dumps({"ok": False, "reason": "invalid_tool_schema",
                           "error": "tool parameter schema is invalid"})
    try:
        return await spec.handler(args, ctx)
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return json.dumps({"ok": False, "error": str(exc)})
