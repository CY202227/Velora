"""Memory summary, archive-kind, and show_memory_hints settings."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.api import memory as memory_api
from server.api import settings as settings_api
from server.config import Settings
from server.core.conversation.store import ConversationStore
from server.core.memory.labels import build_memory_summary, kind_label
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.settings_store import load_settings, save_settings


def test_kind_label_and_summary() -> None:
    assert kind_label("person") == "人物偏好"
    assert kind_label("unknown_xyz") == "unknown_xyz"

    atoms = [
        {"key": "a", "kind": "person", "statement": "称呼老张", "status": "active"},
        {"key": "b", "kind": "person", "statement": "喜欢茶", "status": "active"},
        {"key": "c", "kind": "fact", "statement": "住在上海", "status": "active"},
        {"key": "d", "kind": "fact", "statement": "旧", "status": "archived"},
        {"key": "", "kind": "fact", "statement": "无 key", "status": "active"},
    ]
    summary = build_memory_summary(atoms, per_kind=8)
    assert summary["total"] == 3
    assert "已记住约 3 条" in summary["headline"]
    assert "人物偏好 2" in summary["headline"]
    kinds = {s["kind"]: s for s in summary["sections"]}
    assert len(kinds["person"]["items"]) == 2
    assert kinds["fact"]["items"][0]["statement"] == "住在上海"


def test_summary_empty() -> None:
    summary = build_memory_summary([])
    assert summary["total"] == 0
    assert "还没有记住什么" in summary["headline"]


@pytest.fixture
async def store(tmp_path: Path) -> ConversationStore:
    db = (tmp_path / "t.db").as_posix()
    s = ConversationStore(f"sqlite+aiosqlite:///{db}")
    await s.init()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_show_memory_hints_persist(store: ConversationStore) -> None:
    settings = Settings(show_memory_hints=True)
    provider = OpenAICompatProvider("http://x", "")
    settings.show_memory_hints = False
    await save_settings(store, settings)
    loaded = Settings(show_memory_hints=True)
    await load_settings(store, loaded, provider)
    assert loaded.show_memory_hints is False


@pytest.mark.asyncio
async def test_summary_and_archive_kind_routes() -> None:
    app = FastAPI()
    app.include_router(memory_api.router)

    mem = MagicMock()
    mem.list_atoms = AsyncMock(
        return_value={
            "count": 2,
            "results": [
                {"key": "k1", "kind": "person", "statement": "叫小周", "status": "active"},
                {"key": "k2", "kind": "person", "statement": "喜欢猫", "status": "active"},
            ],
        }
    )
    mem.archive_atom = AsyncMock(return_value={"ok": True})

    state = MagicMock()
    state.settings = Settings(memory_space_uid="velora-test")
    state.memory = mem
    state.consolidate_job = MagicMock()

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.app.state.velora = state
        return await call_next(request)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/memory/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert "人物偏好" in body["headline"]

        r2 = await client.post(
            "/api/memory/atoms/archive-kind", json={"kind": "person"}
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["archived"] == 2
        assert data["failed"] == 0
        assert mem.archive_atom.await_count == 2


@pytest.mark.asyncio
async def test_settings_show_memory_hints_api(store: ConversationStore) -> None:
    app = FastAPI()
    app.include_router(settings_api.router)

    settings = Settings(show_memory_hints=True, llm_model="m")
    provider = OpenAICompatProvider("http://x", "k")
    state = MagicMock()
    state.settings = settings
    state.store = store
    state.provider = provider
    state.memory = MagicMock()
    state.memory._ready = set()

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.app.state.velora = state
        return await call_next(request)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/settings")
        assert r.status_code == 200
        assert r.json()["show_memory_hints"] is True

        r2 = await client.put("/api/settings", json={"show_memory_hints": False})
        assert r2.status_code == 200
        assert r2.json()["show_memory_hints"] is False
        assert settings.show_memory_hints is False
