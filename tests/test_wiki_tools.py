import json
import pytest
from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.tools.registry import ToolRegistry
from server.core.tools.wiki import register_wiki_tools


def test_wiki_tools_disabled_by_default():
    registry = ToolRegistry()
    register_wiki_tools(registry, Settings())
    assert registry.names() == []


@pytest.mark.asyncio
async def test_readonly_wiki_tools_use_real_service(tmp_path):
    pytest.importorskip("xwiki")
    from xwiki import XWikiConfig, XWikiService
    service = XWikiService(XWikiConfig(workspace=str(tmp_path), llm_api_key="", llm_base_url=""))
    (service.workspace.paths.inbox_dir / "tea.md").write_text("# Tea\n\nGreen tea contains caffeine.", encoding="utf-8")
    await service.ingest_inbox()
    service.update_page("Tea", "Green tea contains caffeine.", summary="Tea facts", base_version=None, reason="test")
    registry = ToolRegistry()
    register_wiki_tools(registry, Settings(xwiki_enabled=True), service=service)
    ctx = TurnContext(request=TurnRequest("s", "tea"), session_id="s")
    search = json.loads(await registry.get("wiki_search").handler({"query": "tea"}, ctx))
    assert search["ok"] and search["data"]["documents"]
    read = json.loads(await registry.get("wiki_read").handler({"page": "Tea"}, ctx))
    assert read["ok"] and "caffeine" in read["data"]["page"]["body"]
    answer = json.loads(await registry.get("wiki_ask").handler({"query": "caffeine"}, ctx))
    assert answer["ok"]
    assert registry.names() == ["wiki_search", "wiki_read", "wiki_ask"]
    bad = json.loads(await registry.get("wiki_search").handler({"query": "tea", "limit": 100}, ctx))
    assert bad["ok"] is False
    missing = json.loads(await registry.get("wiki_read").handler({"page": "missing"}, ctx))
    assert missing["ok"] is False
