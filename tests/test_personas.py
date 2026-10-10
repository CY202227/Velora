"""Persona / chara_card_v2 unit tests."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.core.conversation.store import ConversationStore
from server.core.persona.chara_png import extract_chara_json_from_png
from server.core.persona.chara_v2 import (
    CharaCardError,
    parse_chara_card_json,
    parse_mes_example,
)
from server.core.persona.default import BUILTIN_PERSONA_ID, DEFAULT_PERSONA
from server.core.persona.lorebook import scan_character_book
from server.core.persona.macros import apply_macros
from server.core.persona.resolve import prepare_persona_for_prompt, resolve_persona
from server.main import create_app


@pytest.fixture
async def store(tmp_path: Path) -> ConversationStore:
    db = (tmp_path / "t.db").as_posix()
    s = ConversationStore(f"sqlite+aiosqlite:///{db}")
    await s.init()
    yield s
    await s.close()


def test_macros() -> None:
    assert apply_macros("Hi {{user}}, I am {{char}}", char_name="Fuka") == (
        "Hi 用户, I am Fuka"
    )
    assert apply_macros("X {{original}}", char_name="A", original="ORIG") == "X ORIG"


def test_parse_mes_example() -> None:
    raw = (
        "<START>\n{{user}}: Hi\n{{char}}: Hello there\n"
        "<START>\n{{user}}: Bye\n{{char}}: See you"
    )
    d = parse_mes_example(raw)
    assert d == ["Hi", "Hello there", "Bye", "See you"]


def test_parse_fuka_like_card() -> None:
    payload = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "Fuka Shikuzaki",
            "description": "{{char}} is unlucky.",
            "personality": "",
            "scenario": "",
            "first_mes": "H-Hi.",
            "mes_example": "",
            "alternate_greetings": ["Another hello"],
            "system_prompt": "",
            "post_history_instructions": "Stay in character.",
            "character_book": {
                "entries": [
                    {
                        "keys": ["library"],
                        "content": "She likes quiet libraries.",
                        "enabled": True,
                        "insertion_order": 0,
                        "extensions": {},
                    }
                ],
                "extensions": {},
            },
            "creator_notes": "wholesome",
            "tags": ["OC"],
            "creator": "shyzombie",
            "character_version": "main",
            "extensions": {},
        },
    }
    p = parse_chara_card_json(payload, persona_id="p1")
    assert p.name == "Fuka Shikuzaki"
    assert "unlucky" in p.system_prompt or "Fuka" in p.system_prompt
    assert p.opener == "H-Hi."
    assert p.alternate_greetings == ["Another hello"]
    assert p.character_book is not None
    prepared = prepare_persona_for_prompt(p)
    assert "Fuka Shikuzaki" in prepared.system_prompt
    assert "{{char}}" not in prepared.system_prompt


def test_lorebook_constant_and_key() -> None:
    book = {
        "scan_depth": 4,
        "token_budget": 500,
        "entries": [
            {
                "keys": [],
                "content": "ALWAYS",
                "enabled": True,
                "constant": True,
                "insertion_order": 0,
                "position": "before_char",
                "extensions": {},
            },
            {
                "keys": ["厕所"],
                "content": "Bathroom reminder lore",
                "enabled": True,
                "insertion_order": 1,
                "position": "after_char",
                "extensions": {},
            },
        ],
        "extensions": {},
    }
    before, after = scan_character_book(
        book, history_texts=["hello"], user_text="去厕所"
    )
    assert "ALWAYS" in before
    assert "Bathroom" in after


def test_lorebook_budget_drops_low_priority() -> None:
    book = {
        "token_budget": 5,  # ~20 chars
        "entries": [
            {
                "keys": ["a"],
                "content": "AAAAAAAAAA",
                "enabled": True,
                "insertion_order": 0,
                "priority": 1,
                "position": "after_char",
            },
            {
                "keys": ["a"],
                "content": "BBBBBBBBBBBBBBBBBBBB",
                "enabled": True,
                "insertion_order": 1,
                "priority": 99,
                "position": "after_char",
            },
        ],
    }
    _before, after = scan_character_book(
        book, history_texts=[], user_text="a"
    )
    assert "AAAAAAAAAA" in after
    assert "BBBB" not in after


@pytest.mark.asyncio
async def test_compose_includes_lore_and_post_history() -> None:
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest
    from server.core.nodes.compose_prompt import ComposePromptNode
    from server.core.persona.default import Persona

    persona = Persona(
        id="lore-p",
        name="LoreChar",
        system_prompt="CHAR BODY",
        source="import",
        post_history_instructions="Stay shy.",
        character_book={
            "entries": [
                {
                    "keys": ["library"],
                    "content": "LORE_HIT",
                    "enabled": True,
                    "insertion_order": 0,
                    "position": "before_char",
                }
            ]
        },
    )
    ctx = TurnContext(
        request=TurnRequest(session_id="s", user_text="go to the library"),
        session_id="s",
        persona=persona,
        history=[],
        style_knobs={"warmth": 35},
    )
    await ComposePromptNode().process(ctx)
    system = ctx.messages[0]["content"]
    assert "LORE_HIT" in system
    assert "CHAR BODY" in system
    assert any(
        m.get("role") == "system" and m.get("content") == "Stay shy."
        for m in ctx.messages[1:]
    )


def _png_with_chara(payload: dict) -> bytes:
    """Minimal PNG + tEXt chara chunk."""
    # 1x1 PNG IHDR + IDAT + IEND skeleton then we rebuild with text
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0),
    )
    # empty IDAT
    idat = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00"))
    text = base64.b64encode(json.dumps(payload).encode("utf-8"))
    text_chunk = chunk(b"tEXt", b"chara\x00" + text)
    iend = chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + text_chunk + iend


def test_png_chara_extract() -> None:
    payload = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "PngChar",
            "description": "from png",
            "personality": "",
            "scenario": "",
            "first_mes": "yo",
            "mes_example": "",
            "alternate_greetings": [],
            "system_prompt": "",
            "post_history_instructions": "",
            "tags": [],
            "creator": "",
            "character_version": "",
            "extensions": {},
            "creator_notes": "",
        },
    }
    raw = _png_with_chara(payload)
    got = extract_chara_json_from_png(raw)
    assert got["data"]["name"] == "PngChar"


@pytest.mark.asyncio
async def test_resolve_and_store(store: ConversationStore) -> None:
    p = parse_chara_card_json(
        {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "Stored",
                "description": "d",
                "personality": "",
                "scenario": "",
                "first_mes": "hi",
                "mes_example": "",
                "alternate_greetings": ["alt"],
                "system_prompt": "",
                "post_history_instructions": "",
                "tags": [],
                "creator": "",
                "character_version": "",
                "extensions": {},
                "creator_notes": "",
            },
        },
        persona_id="abc",
    )
    await store.upsert_persona(p)
    got = await resolve_persona(store, "abc")
    assert got.name == "Stored"
    assert await resolve_persona(store, "missing") is DEFAULT_PERSONA
    assert (await resolve_persona(store, BUILTIN_PERSONA_ID)).id == BUILTIN_PERSONA_ID


@pytest.mark.asyncio
async def test_personas_api_import_json(tmp_path: Path, monkeypatch) -> None:
    db = (tmp_path / "api.db").as_posix()
    monkeypatch.setenv("VELORA_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("VELORA_START_MEMORY_SIDECAR", "false")
    # recreate settings
    from server import config as config_mod

    config_mod.get_settings.cache_clear() if hasattr(
        config_mod.get_settings, "cache_clear"
    ) else None
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # lifespan
        async with app.router.lifespan_context(app):
            listed = await client.get("/api/personas")
            assert listed.status_code == 200
            assert any(p["id"] == BUILTIN_PERSONA_ID for p in listed.json())
            card = {
                "spec": "chara_card_v2",
                "spec_version": "2.0",
                "data": {
                    "name": "ApiChar",
                    "description": "{{char}} waves",
                    "personality": "shy",
                    "scenario": "school",
                    "first_mes": "Hi",
                    "mes_example": "",
                    "alternate_greetings": ["Yo"],
                    "system_prompt": "",
                    "post_history_instructions": "",
                    "tags": [],
                    "creator": "",
                    "character_version": "",
                    "extensions": {},
                    "creator_notes": "",
                },
            }
            imp = await client.post("/api/personas/import", json=card)
            assert imp.status_code == 200, imp.text
            body = imp.json()
            assert body["name"] == "ApiChar"
            assert "Yo" in body["greetings"]
            pid = body["id"]
            sess = await client.post(
                "/api/sessions", json={"persona_id": pid, "warmth": 40}
            )
            assert sess.status_code == 200
            assert sess.json()["persona_id"] == pid
            assert sess.json()["persona_name"] == "ApiChar"
