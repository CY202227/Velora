import json
from unittest.mock import AsyncMock

import pytest

from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.tools import ToolRegistry, execute_tool
from server.core.tools.types import ToolSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"count": True}, {"count": 0}, {"count": 2, "extra": 1}, [], False])
async def test_invalid_arguments_never_reach_handler(args):
    handler = AsyncMock(return_value='{"ok":true}')
    registry = ToolRegistry()
    registry.register(ToolSpec("count", "count", {
        "type": "object", "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"], "additionalProperties": False,
    }, handler))
    ctx = TurnContext(request=TurnRequest("s", "test"), session_id="s")
    assert json.loads(await execute_tool(registry, "count", args, ctx))["ok"] is False
    handler.assert_not_awaited()


def test_duplicate_registration_preserves_original_handler():
    registry = ToolRegistry()
    original = ToolSpec("same", "original")
    registry.register(original)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ToolSpec("same", "replacement"))
    assert registry.get("same") is original
