"""Reminders: quiet hours, parse, cron, claim, chain wake, attachments."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.api import chat as chat_api
from server.api import reminders as reminders_api
from server.api import sessions as sessions_api
from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import TurnRequest
from server.core.conversation.store import ConversationStore
from server.core.reminders.parse import parse_reminder_line
from server.core.reminders.quiet import in_quiet_hours, next_quiet_end_utc
from server.core.reminders.runner import deliver_reminder
from server.core.reminders.schedule import CronExprError, next_fire_utc, validate_cron_expr
from server.core.computer.attachments import diff_attachments, snapshot_workspace
from server.core.computer.workspace import workspace_root


@pytest.fixture
async def store(tmp_path: Path) -> ConversationStore:
    db = (tmp_path / "r.db").as_posix()
    s = ConversationStore(f"sqlite+aiosqlite:///{db}")
    await s.init()
    yield s
    await s.close()


def test_parse_reminder_line() -> None:
    p = parse_reminder_line("请提醒：明天下午开会 | 2026-08-06T15:00:00+08:00")
    assert p is not None
    assert p.note == "明天下午开会"
    assert p.due_at == datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc)

    assert parse_reminder_line("随便聊聊") is None
    assert parse_reminder_line("请提醒：没有时间") is None


def test_quiet_hours_shanghai_overnight() -> None:
    when = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert in_quiet_hours(
        when,
        tz_name="Asia/Shanghai",
        start="22:00",
        end="08:00",
    )
    end_utc = next_quiet_end_utc(
        when,
        tz_name="Asia/Shanghai",
        start="22:00",
        end="08:00",
    )
    assert end_utc == datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)

    afternoon = datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc)
    assert not in_quiet_hours(
        afternoon,
        tz_name="Asia/Shanghai",
        start="22:00",
        end="08:00",
    )


def test_quiet_hours_new_york() -> None:
    when = datetime(2026, 1, 16, 4, 30, tzinfo=timezone.utc)
    assert in_quiet_hours(
        when,
        tz_name="America/New_York",
        start="22:00",
        end="08:00",
    )
    end_utc = next_quiet_end_utc(
        when,
        tz_name="America/New_York",
        start="22:00",
        end="08:00",
    )
    assert end_utc == datetime(2026, 1, 16, 13, 0, tzinfo=timezone.utc)


def test_cron_next_fire_utc() -> None:
    validate_cron_expr("0 9 * * *")
    with pytest.raises(CronExprError):
        validate_cron_expr("bad")
    after = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_utc("0 9 * * *", tz_name="Asia/Shanghai", after=after)
    # 09:00 Asia/Shanghai = 01:00 UTC
    assert nxt.hour == 1
    assert nxt.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_due_and_cancel(store: ConversationStore) -> None:
    sess = await store.create_session(
        persona_id="p",
        memory_space_uid="u",
        model="m",
    )
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    r1 = await store.create_reminder(session_id=sess.id, note="a", due_at=past)
    await store.create_reminder(session_id=sess.id, note="b", due_at=future)
    due = await store.list_due_reminders(now=datetime(2021, 1, 1, tzinfo=timezone.utc))
    assert [d.note for d in due] == ["a"]
    cancelled = await store.cancel_reminder(r1.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    due2 = await store.list_due_reminders(now=datetime(2021, 1, 1, tzinfo=timezone.utc))
    assert due2 == []


@pytest.mark.asyncio
async def test_claim_prevents_double_delivery(store: ConversationStore) -> None:
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    rem = await store.create_reminder(
        session_id=sess.id,
        note="once",
        due_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    first = await store.claim_due_reminder(rem.id)
    second = await store.claim_due_reminder(rem.id)
    assert first is not None
    assert first.status == "running"
    assert second is None


class FakeWaitRegistry:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear(self, session_id: str) -> None:
        self.cleared.append(session_id)


class FakeRouter:
    def __init__(self, text: str = "任务完成。") -> None:
        self.text = text
        self.wait_registry = FakeWaitRegistry()
        self.calls: list[TurnRequest] = []

    async def handle(self, request: TurnRequest, **kwargs):
        del kwargs
        self.calls.append(request)
        ctx = TurnContext(request=request, session_id=request.session_id)
        ctx.assistant_text = self.text
        # Simulate PersistTurn writing reminder turns
        return ctx


@pytest.mark.asyncio
async def test_runner_once_via_router(store: ConversationStore) -> None:
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    rem = await store.create_reminder(
        session_id=sess.id,
        note="喝水",
        due_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    async def handle(request: TurnRequest, **kwargs):
        del kwargs
        assert request.client_meta.get("proactive") is True
        assert request.client_meta.get("reminder_note") == "喝水"
        await store.add_turn(
            sess.id, "assistant", "该喝水啦，起身喝一杯吧。", source="reminder"
        )
        ctx = TurnContext(request=request, session_id=request.session_id)
        ctx.assistant_text = "该喝水啦，起身喝一杯吧。"
        return ctx

    router = FakeRouter()
    router.handle = handle  # type: ignore[method-assign]
    settings = Settings(quiet_hours_enabled=False, reminders_enabled=True)
    ok = await deliver_reminder(
        rem, store=store, settings=settings, router=router  # type: ignore[arg-type]
    )
    assert ok is True
    updated = await store.get_reminder(rem.id)
    assert updated is not None
    assert updated.status == "delivered"
    assert updated.deliver_text == "该喝水啦，起身喝一杯吧。"
    turns = await store.list_turns(sess.id)
    assert any(t.source == "reminder" and t.role == "assistant" for t in turns)


@pytest.mark.asyncio
async def test_runner_cron_advances_due(store: ConversationStore) -> None:
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    rem = await store.create_reminder(
        session_id=sess.id,
        note="日报",
        due_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        schedule_kind="cron",
        cron_expr="0 9 * * *",
    )
    router = FakeRouter(text="日报已整理。")
    settings = Settings(
        quiet_hours_enabled=False,
        reminders_enabled=True,
        user_timezone="UTC",
    )
    ok = await deliver_reminder(
        rem, store=store, settings=settings, router=router  # type: ignore[arg-type]
    )
    assert ok is True
    updated = await store.get_reminder(rem.id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.due_at > rem.due_at
    assert updated.deliver_text == "日报已整理。"
    assert router.wait_registry.cleared == [sess.id]


@pytest.mark.asyncio
async def test_runner_defers_in_quiet_hours(store: ConversationStore) -> None:
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    rem = await store.create_reminder(
        session_id=sess.id,
        note="深夜勿扰",
        due_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    router = FakeRouter()
    settings = Settings(
        quiet_hours_enabled=True,
        user_timezone="UTC",
        quiet_hours_start="00:00",
        quiet_hours_end="23:59",
        llm_model="test-model",
    )
    ok = await deliver_reminder(
        rem, store=store, settings=settings, router=router  # type: ignore[arg-type]
    )
    assert ok is True
    assert router.calls == []
    turns = await store.list_turns(sess.id)
    assert turns == []
    updated = await store.get_reminder(rem.id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.due_at > rem.due_at


@pytest.mark.asyncio
async def test_chat_shortcut_creates_reminder(store: ConversationStore) -> None:
    settings = Settings(quiet_hours_enabled=False, reminders_enabled=True)
    router = AsyncMock()
    router.handle = AsyncMock()
    app = FastAPI()
    app.include_router(chat_api.router)
    app.include_router(reminders_api.router)
    app.state.velora = SimpleNamespace(settings=settings, store=store, router=router)

    sess = await store.create_session(persona_id="p", memory_space_uid="u", model="m")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/chat/stream",
            json={
                "session_id": sess.id,
                "text": "请提醒：买菜 | 2026-08-06T15:00:00+08:00",
            },
        )
        assert res.status_code == 200
        body = res.text
        assert "已设提醒" in body or "reminder_created" in body
        listed = await client.get(f"/api/reminders?session_id={sess.id}&status=pending")
        assert listed.status_code == 200
        data = listed.json()
        assert len(data) == 1
        assert data[0]["note"] == "买菜"
        assert data[0].get("schedule_kind", "once") == "once"
    router.handle.assert_not_called()


@pytest.mark.asyncio
async def test_api_create_cron_reminder(store: ConversationStore) -> None:
    settings = Settings(quiet_hours_enabled=False, user_timezone="UTC")
    app = FastAPI()
    app.include_router(reminders_api.router)
    app.state.velora = SimpleNamespace(settings=settings, store=store)
    sess = await store.create_session(persona_id="p", memory_space_uid="u", model="m")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/reminders",
            json={
                "session_id": sess.id,
                "note": "站会",
                "cron_expr": "30 9 * * 1-5",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["schedule_kind"] == "cron"
        assert data["cron_expr"] == "30 9 * * 1-5"
        assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_workspace_file_download_and_escape(
    store: ConversationStore, tmp_path: Path
) -> None:
    ws = tmp_path / "workspaces"
    settings = Settings(workspaces_dir=str(ws))
    app = FastAPI()
    app.include_router(sessions_api.router)
    app.state.velora = SimpleNamespace(settings=settings, store=store, memory=AsyncMock())
    sess = await store.create_session(persona_id="p", memory_space_uid="u", model="m")
    root = workspace_root(str(ws), sess.id)
    (root / "out.txt").write_text("hello", encoding="utf-8")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get(f"/api/sessions/{sess.id}/workspace/out.txt")
        assert ok.status_code == 200
        assert ok.text == "hello"
        bad = await client.get(
            f"/api/sessions/{sess.id}/workspace/nested/../../secret.txt"
        )
        assert bad.status_code in (400, 404)


@pytest.mark.asyncio
async def test_attachment_diff(tmp_path: Path) -> None:
    ws = tmp_path / "workspaces"
    sid = "sess-attach"
    before = snapshot_workspace(str(ws), sid)
    root = workspace_root(str(ws), sid)
    (root / "report.md").write_text("# ok", encoding="utf-8")
    (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    atts = diff_attachments(str(ws), sid, before)
    paths = {a["path"] for a in atts}
    assert "report.md" in paths
    assert "pic.png" in paths
    kinds = {a["path"]: a["kind"] for a in atts}
    assert kinds["pic.png"] == "image"
    assert kinds["report.md"] == "file"


@pytest.mark.asyncio
async def test_quiet_settings_persist(store: ConversationStore) -> None:
    from server.core.provider.openai_compat import OpenAICompatProvider
    from server.core.settings_store import load_settings, save_settings

    settings = Settings(
        user_timezone="America/New_York",
        quiet_hours_enabled=False,
        quiet_hours_start="23:00",
        quiet_hours_end="07:00",
    )
    provider = OpenAICompatProvider("http://x", "")
    await save_settings(store, settings)
    loaded = Settings()
    await load_settings(store, loaded, provider)
    assert loaded.user_timezone == "America/New_York"
    assert loaded.quiet_hours_enabled is False
    assert loaded.quiet_hours_start == "23:00"
    assert loaded.quiet_hours_end == "07:00"


def test_finalize_reminder_reply_rewrites_setup_echo() -> None:
    from server.core.reminders.text import finalize_reminder_reply

    assert finalize_reminder_reply("去上厕所", "该起身去上厕所了。") == (
        "该起身去上厕所了。"
    )
    assert finalize_reminder_reply(
        "去上厕所",
        "已设置 5 分钟后的提醒，届时我会提醒你。",
    ) == "嘿，到点了——该去上厕所啦。"
    assert finalize_reminder_reply(
        "去上厕所",
        "收到。已为你设置定时提醒：16:45 去上厕所",
    ) == "嘿，到点了——该去上厕所啦。"
    assert finalize_reminder_reply("开会", "") == "嘿，到点了——该开会啦。"


@pytest.mark.asyncio
async def test_runner_rewrites_setup_echo_from_llm(
    store: ConversationStore,
) -> None:
    sess = await store.create_session(
        persona_id="p", memory_space_uid="u", model="m"
    )
    rem = await store.create_reminder(
        session_id=sess.id,
        note="去上厕所",
        due_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    router = FakeRouter(text="已设置 5 分钟后的提醒")
    settings = Settings(quiet_hours_enabled=False, reminders_enabled=True)
    ok = await deliver_reminder(
        rem, store=store, settings=settings, router=router  # type: ignore[arg-type]
    )
    assert ok is True
    updated = await store.get_reminder(rem.id)
    assert updated is not None
    assert updated.deliver_text == "嘿，到点了——该去上厕所啦。"
    assert len(router.calls) == 1
