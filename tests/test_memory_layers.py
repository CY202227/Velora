"""Layered recall request + slower L2/L3 cadence after scheduled consolidate."""

from __future__ import annotations

import pytest

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult, TurnRequest
from server.core.memory.client import AtomMemoryClient
from server.core.memory.consolidate import ConsolidateJob
from server.core.nodes.recall_memory import RecallMemoryNode


class _Resp:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _Http:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_Http":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def post(self, url: str, headers=None, json=None):
        del headers
        self.posts.append((url, json or {}))
        return _Resp(self.payload)


class LayerMemory:
    def __init__(self) -> None:
        self.consolidates: list[str] = []
        self.synthesizes = 0
        self.personas = 0
        self.recall_kwargs: dict = {}

    async def recall(self, uid: str, query: str, **kwargs):
        del uid
        self.recall_kwargs = {"query": query, **kwargs}
        return {"context_block": "<recalled_memory/>", "hits": [], "chars_used": 0}

    async def consolidate(self, uid: str, *, trigger: str = "manual"):
        del uid
        self.consolidates.append(trigger)
        return {"status": "succeeded"}

    async def synthesize(self, uid: str, **kwargs):
        del uid, kwargs
        self.synthesizes += 1
        return {"status": "succeeded"}

    async def persona(self, uid: str, **kwargs):
        del uid, kwargs
        self.personas += 1
        return {"status": "succeeded"}


@pytest.mark.asyncio
async def test_recall_sends_layered_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _Http({"context_block": "x", "hits": []})
    monkeypatch.setattr(
        "server.core.memory.client.httpx.AsyncClient",
        lambda **kwargs: http,
    )
    client = AtomMemoryClient("http://memory.test")
    client._ready.add("space-1")
    result = await client.recall("space-1", "我叫老张")
    assert result["context_block"] == "x"
    assert len(http.posts) == 1
    url, body = http.posts[0]
    assert url.endswith("/spaces/space-1/recall")
    assert body["policy"] == "layered"
    assert body["detail"] == "statement"
    assert body["neighbor_hops"] == 0


@pytest.mark.asyncio
async def test_recall_node_passes_policy() -> None:
    memory = LayerMemory()
    node = RecallMemoryNode(memory, policy="layered")  # type: ignore[arg-type]
    ctx = TurnContext(
        request=TurnRequest("s", "还记得我喜欢茶吗"),
        session_id="s",
        memory_space_uid="u",
    )
    assert await node.process(ctx) is NodeResult.CONTINUE
    assert memory.recall_kwargs["policy"] == "layered"
    assert ctx.memory_block == "<recalled_memory/>"


@pytest.mark.asyncio
async def test_explicit_correction_skips_stale_memory_recall() -> None:
    memory = LayerMemory()
    events = []

    async def emit(event):
        events.append(event)

    ctx = TurnContext(
        request=TurnRequest("s", "请记住我现在改喝茶"),
        session_id="s",
        memory_space_uid="u",
        is_correction=True,
        emit=emit,
    )
    assert await RecallMemoryNode(memory).process(ctx) is NodeResult.SKIP  # type: ignore[arg-type]
    assert memory.recall_kwargs == {}
    assert ctx.memory_block == ""
    assert events[0].data["skipped"] == "correction"


@pytest.mark.asyncio
async def test_recall_event_exposes_only_safe_hit_fields() -> None:
    class ExplainableMemory(LayerMemory):
        async def recall(self, uid: str, query: str, **kwargs):
            del uid, query, kwargs
            return {
                "context_block": "<recalled_memory>internal prompt</recalled_memory>",
                "hits": [
                    {
                        "key": "preference-coffee",
                        "kind": "preference",
                        "statement": "喜欢手冲咖啡",
                        "memory_layer": 1,
                        "detail": "Do not expose this detail",
                    }
                ],
            }

    events = []

    async def emit(event):
        events.append(event)

    ctx = TurnContext(
        request=TurnRequest("s", "我喜欢什么？"),
        session_id="s",
        memory_space_uid="u",
        emit=emit,
    )
    await RecallMemoryNode(ExplainableMemory()).process(ctx)  # type: ignore[arg-type]

    data = events[0].data
    assert data["items"] == [
        {
            "key": "preference-coffee",
            "statement": "喜欢手冲咖啡",
            "kind": "preference",
            "memory_layer": 1,
        }
    ]
    assert "context_block" not in data


@pytest.mark.asyncio
async def test_layers_run_every_n_scheduled() -> None:
    memory = LayerMemory()
    job = ConsolidateJob(memory, layer_every_n=2)  # type: ignore[arg-type]
    await job.run_now("u", trigger="correction")
    assert memory.consolidates == ["correction"]
    assert memory.synthesizes == 0
    assert memory.personas == 0

    await job.run_now("u", trigger="scheduled")
    await job.run_now("u", trigger="scheduled")
    assert memory.synthesizes == 1
    assert memory.personas == 1

    await job.run_now("u", trigger="scheduled")
    await job.run_now("u", trigger="scheduled")
    assert memory.synthesizes == 2
    assert memory.personas == 2


@pytest.mark.asyncio
async def test_layers_disabled_when_every_n_zero() -> None:
    memory = LayerMemory()
    job = ConsolidateJob(memory, layer_every_n=0)  # type: ignore[arg-type]
    await job.run_now("u", trigger="scheduled")
    assert memory.synthesizes == 0
    assert memory.personas == 0
