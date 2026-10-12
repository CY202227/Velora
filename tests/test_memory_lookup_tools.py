import json
from unittest.mock import AsyncMock
import pytest
from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.persona.default import DEFAULT_PERSONA
from server.core.tools.registry import ToolRegistry
from server.core.tools.builtin import register_builtin_tools
from server.core.nodes.compose_prompt import ComposePromptNode


@pytest.mark.asyncio
async def test_memory_tools_search_then_expand():
    registry = ToolRegistry()
    register_builtin_tools(registry)
    memory = AsyncMock()
    memory.recall.return_value = {"hits": [{"key": "profile", "statement": "a short fact"}], "context_block": "fact"}
    memory.expand.return_value = {"hits": [{"key": "profile", "detail": "long fact"}]}
    memory.search_sources.return_value = {"hits": [{"source_id": 1, "excerpt": "original fact"}], "evidence_status": "historical_sources"}
    ctx = TurnContext(request=TurnRequest("s", "identity"), session_id="s", persona=DEFAULT_PERSONA, memory_space_uid="space")
    ctx.extras.update(memory=memory, settings=Settings())
    result = json.loads(await registry.get("memory_search").handler({"query": "identity", "method": "llm"}, ctx))
    assert result["ok"]
    assert result["source_evidence"]["evidence_status"] == "historical_sources"
    memory.search_sources.assert_awaited_once_with("space", "identity", budget_chars=2400)
    assert memory.recall.call_args.kwargs["method"] == "llm"
    details = json.loads(await registry.get("memory_expand").handler({"keys": ["profile"]}, ctx))
    assert details["ok"]
    memory.expand.assert_awaited_once_with("space", ["profile"], budget_chars=2400)
    await ComposePromptNode(registry).process(ctx)
    assert "memory_search" in ctx.messages[0]["content"]
    assert "memory_expand" in ctx.messages[0]["content"]
    bad = json.loads(await registry.get("memory_expand").handler({"keys": []}, ctx))
    assert bad["ok"] is False
