"""Session / turn / app_settings persistence (SQLite or MySQL via SQLAlchemy)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    select,
    func,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_database_url(url: str) -> str:
    """Accept common URL forms; return SQLAlchemy async URL."""
    u = url.strip()
    if u.startswith("mysql://"):
        return "mysql+aiomysql://" + u[len("mysql://") :]
    if u.startswith("mysql+pymysql://"):
        return "mysql+aiomysql://" + u[len("mysql+pymysql://") :]
    if u.startswith("sqlite:///") and "+aiosqlite" not in u:
        return "sqlite+aiosqlite:///" + u[len("sqlite:///") :]
    return u


@dataclass
class SessionRow:
    id: str
    persona_id: str
    memory_space_uid: str
    model: str
    tts_enabled: bool
    style_knobs: dict
    created_at: str
    updated_at: str


@dataclass
class TurnRow:
    id: str
    session_id: str
    role: str
    content: str
    created_at: str
    source: str = "chat"
    attachments: list[dict] | None = None


@dataclass
class ReminderRow:
    id: str
    session_id: str
    note: str
    due_at: str
    status: str
    deliver_text: str | None
    created_at: str
    delivered_at: str | None
    error: str | None
    schedule_kind: str = "once"
    cron_expr: str | None = None
    last_run_at: str | None = None


class Base(DeclarativeBase):
    pass


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_space_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    tts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    style_knobs: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TurnModel(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    attachments: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class ReminderModel(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_status_due", "status", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id"), nullable=False, index=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    deliver_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="once"
    )
    cron_expr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AppSettingModel(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _session_row(m: SessionModel) -> SessionRow:
    return SessionRow(
        id=m.id,
        persona_id=m.persona_id,
        memory_space_uid=m.memory_space_uid,
        model=m.model,
        tts_enabled=bool(m.tts_enabled),
        style_knobs=json.loads(m.style_knobs or "{}"),
        created_at=_iso(m.created_at),
        updated_at=_iso(m.updated_at),
    )


def _parse_attachments(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _turn_row(m: TurnModel) -> TurnRow:
    return TurnRow(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        created_at=_iso(m.created_at),
        source=getattr(m, "source", None) or "chat",
        attachments=_parse_attachments(getattr(m, "attachments", None)),
    )


def _reminder_row(m: ReminderModel) -> ReminderRow:
    return ReminderRow(
        id=m.id,
        session_id=m.session_id,
        note=m.note,
        due_at=_iso(m.due_at),
        status=m.status,
        deliver_text=m.deliver_text,
        created_at=_iso(m.created_at),
        delivered_at=_iso(m.delivered_at) if m.delivered_at else None,
        error=m.error,
        schedule_kind=getattr(m, "schedule_kind", None) or "once",
        cron_expr=getattr(m, "cron_expr", None),
        last_run_at=_iso(m.last_run_at) if getattr(m, "last_run_at", None) else None,
    )


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_columns(sync_conn) -> None:
    """Add columns introduced after first create_all (no full migrator)."""
    from sqlalchemy import inspect, text

    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())

    def existing(table: str) -> set[str]:
        if table not in tables:
            return set()
        return {c["name"] for c in insp.get_columns(table)}

    turns_cols = existing("turns")
    if "turns" in tables:
        if "source" not in turns_cols:
            sync_conn.execute(
                text(
                    "ALTER TABLE turns ADD COLUMN source VARCHAR(32) "
                    "NOT NULL DEFAULT 'chat'"
                )
            )
        if "attachments" not in turns_cols:
            sync_conn.execute(
                text(
                    "ALTER TABLE turns ADD COLUMN attachments TEXT "
                    "NOT NULL DEFAULT '[]'"
                )
            )

    rem_cols = existing("reminders")
    if "reminders" in tables:
        if "schedule_kind" not in rem_cols:
            sync_conn.execute(
                text(
                    "ALTER TABLE reminders ADD COLUMN schedule_kind "
                    "VARCHAR(16) NOT NULL DEFAULT 'once'"
                )
            )
        if "cron_expr" not in rem_cols:
            sync_conn.execute(
                text("ALTER TABLE reminders ADD COLUMN cron_expr VARCHAR(128)")
            )
        if "last_run_at" not in rem_cols:
            sync_conn.execute(
                text("ALTER TABLE reminders ADD COLUMN last_run_at DATETIME")
            )


class ConversationStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = normalize_database_url(database_url)
        if self.database_url.startswith("sqlite+aiosqlite:///"):
            path = Path(self.database_url.removeprefix("sqlite+aiosqlite:///"))
            # Windows absolute: /D:/... from URL sometimes
            if path.as_posix().startswith("/") and len(path.as_posix()) > 2 and path.as_posix()[2] == ":":
                path = Path(path.as_posix()[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(self.database_url, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_columns)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_session(
        self,
        *,
        persona_id: str,
        memory_space_uid: str,
        model: str,
        tts_enabled: bool = False,
        style_knobs: dict | None = None,
    ) -> SessionRow:
        sid = str(uuid.uuid4())
        now = _utcnow()
        row = SessionModel(
            id=sid,
            persona_id=persona_id,
            memory_space_uid=memory_space_uid,
            model=model,
            tts_enabled=tts_enabled,
            style_knobs=json.dumps(style_knobs or {}, ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return _session_row(row)

    async def get_session(self, session_id: str) -> SessionRow | None:
        async with self._session_factory() as db:
            m = await db.get(SessionModel, session_id)
            return _session_row(m) if m else None

    async def list_sessions(self, limit: int = 50) -> list[SessionRow]:
        async with self._session_factory() as db:
            result = await db.scalars(
                select(SessionModel)
                .order_by(SessionModel.updated_at.desc())
                .limit(limit)
            )
            return [_session_row(m) for m in result.all()]

    async def touch_session(self, session_id: str) -> None:
        async with self._session_factory() as db:
            m = await db.get(SessionModel, session_id)
            if m is None:
                return
            m.updated_at = _utcnow()
            await db.commit()

    async def update_session_model(self, session_id: str, model: str) -> None:
        async with self._session_factory() as db:
            m = await db.get(SessionModel, session_id)
            if m is None:
                return
            m.model = model
            m.updated_at = _utcnow()
            await db.commit()

    async def update_session(
        self,
        session_id: str,
        *,
        style_knobs: dict | None = None,
        tts_enabled: bool | None = None,
        model: str | None = None,
    ) -> SessionRow | None:
        async with self._session_factory() as db:
            m = await db.get(SessionModel, session_id)
            if m is None:
                return None
            if style_knobs is not None:
                current = json.loads(m.style_knobs or "{}")
                current.update(style_knobs)
                m.style_knobs = json.dumps(current, ensure_ascii=False)
            if tts_enabled is not None:
                m.tts_enabled = tts_enabled
            if model is not None:
                m.model = model
            m.updated_at = _utcnow()
            await db.commit()
            await db.refresh(m)
            return _session_row(m)

    async def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        source: str = "chat",
        attachments: list[dict] | None = None,
    ) -> TurnRow:
        tid = str(uuid.uuid4())
        now = _utcnow()
        turn = TurnModel(
            id=tid,
            session_id=session_id,
            role=role,
            content=content,
            created_at=now,
            source=source or "chat",
            attachments=json.dumps(attachments or [], ensure_ascii=False),
        )
        async with self._session_factory() as db:
            db.add(turn)
            session = await db.get(SessionModel, session_id)
            if session is not None:
                session.updated_at = now
            await db.commit()
            await db.refresh(turn)
            return _turn_row(turn)

    async def list_turns(
        self, session_id: str, *, limit: int = 40
    ) -> list[TurnRow]:
        async with self._session_factory() as db:
            result = await db.scalars(
                select(TurnModel)
                .where(TurnModel.session_id == session_id)
                .order_by(TurnModel.created_at.desc())
                .limit(limit)
            )
            rows = list(result.all())
            rows.reverse()
            return [_turn_row(m) for m in rows]

    async def count_turns(self, session_id: str) -> int:
        async with self._session_factory() as db:
            n = await db.scalar(
                select(func.count())
                .select_from(TurnModel)
                .where(TurnModel.session_id == session_id)
            )
            return int(n or 0)

    async def get_setting(self, key: str) -> str | None:
        async with self._session_factory() as db:
            m = await db.get(AppSettingModel, key)
            return m.value if m else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._session_factory() as db:
            m = await db.get(AppSettingModel, key)
            if m is None:
                db.add(AppSettingModel(key=key, value=value))
            else:
                m.value = value
            await db.commit()

    async def create_reminder(
        self,
        *,
        session_id: str,
        note: str,
        due_at: datetime,
        schedule_kind: str = "once",
        cron_expr: str | None = None,
    ) -> ReminderRow:
        rid = str(uuid.uuid4())
        now = _utcnow()
        kind = (schedule_kind or "once").strip().lower()
        if kind not in ("once", "cron"):
            kind = "once"
        row = ReminderModel(
            id=rid,
            session_id=session_id,
            note=note.strip(),
            due_at=_as_utc(due_at),
            status="pending",
            deliver_text=None,
            created_at=now,
            delivered_at=None,
            error=None,
            schedule_kind=kind,
            cron_expr=(cron_expr or "").strip() or None,
            last_run_at=None,
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return _reminder_row(row)

    async def get_reminder(self, reminder_id: str) -> ReminderRow | None:
        async with self._session_factory() as db:
            m = await db.get(ReminderModel, reminder_id)
            return _reminder_row(m) if m else None

    async def list_reminders(
        self,
        session_id: str | None = None,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ReminderRow]:
        async with self._session_factory() as db:
            stmt = select(ReminderModel).order_by(ReminderModel.due_at.asc()).limit(limit)
            if session_id:
                stmt = stmt.where(ReminderModel.session_id == session_id)
            if status:
                stmt = stmt.where(ReminderModel.status == status)
            result = await db.scalars(stmt)
            return [_reminder_row(m) for m in result.all()]

    async def list_due_reminders(
        self, *, now: datetime | None = None, limit: int = 5
    ) -> list[ReminderRow]:
        when = _as_utc(now or _utcnow())
        async with self._session_factory() as db:
            result = await db.scalars(
                select(ReminderModel)
                .where(
                    ReminderModel.status == "pending",
                    ReminderModel.due_at <= when,
                )
                .order_by(ReminderModel.due_at.asc())
                .limit(limit)
            )
            return [_reminder_row(m) for m in result.all()]

    async def reschedule_reminder(
        self, reminder_id: str, due_at: datetime
    ) -> ReminderRow | None:
        async with self._session_factory() as db:
            m = await db.get(ReminderModel, reminder_id)
            if m is None:
                return None
            m.due_at = _as_utc(due_at)
            await db.commit()
            await db.refresh(m)
            return _reminder_row(m)

    async def claim_due_reminder(
        self, reminder_id: str, *, now: datetime | None = None
    ) -> ReminderRow | None:
        """Atomically claim a pending due reminder as running."""
        when = _as_utc(now or _utcnow())
        async with self._session_factory() as db:
            result = await db.execute(
                update(ReminderModel)
                .where(
                    ReminderModel.id == reminder_id,
                    ReminderModel.status == "pending",
                    ReminderModel.due_at <= when,
                )
                .values(status="running", error=None)
            )
            await db.commit()
            if int(result.rowcount or 0) != 1:
                return None
            m = await db.get(ReminderModel, reminder_id)
            return _reminder_row(m) if m else None

    async def mark_reminder_delivered(
        self, reminder_id: str, deliver_text: str
    ) -> ReminderRow | None:
        async with self._session_factory() as db:
            m = await db.get(ReminderModel, reminder_id)
            if m is None:
                return None
            now = _utcnow()
            m.status = "delivered"
            m.deliver_text = deliver_text
            m.delivered_at = now
            m.last_run_at = now
            m.error = None
            await db.commit()
            await db.refresh(m)
            return _reminder_row(m)

    async def complete_cron_run(
        self,
        reminder_id: str,
        *,
        next_due_at: datetime,
        deliver_text: str,
    ) -> ReminderRow | None:
        """After a cron fire: keep pending and advance due_at."""
        async with self._session_factory() as db:
            m = await db.get(ReminderModel, reminder_id)
            if m is None:
                return None
            now = _utcnow()
            m.status = "pending"
            m.due_at = _as_utc(next_due_at)
            m.deliver_text = deliver_text
            m.last_run_at = now
            m.error = None
            await db.commit()
            await db.refresh(m)
            return _reminder_row(m)

    async def mark_reminder_failed(
        self,
        reminder_id: str,
        error: str,
        *,
        next_due_at: datetime | None = None,
        keep_pending: bool = False,
    ) -> ReminderRow | None:
        async with self._session_factory() as db:
            m = await db.get(ReminderModel, reminder_id)
            if m is None:
                return None
            m.error = error[:2000]
            m.last_run_at = _utcnow()
            if keep_pending and next_due_at is not None:
                m.status = "pending"
                m.due_at = _as_utc(next_due_at)
            else:
                m.status = "failed"
            await db.commit()
            await db.refresh(m)
            return _reminder_row(m)

    async def cancel_reminder(self, reminder_id: str) -> ReminderRow | None:
        async with self._session_factory() as db:
            m = await db.get(ReminderModel, reminder_id)
            if m is None:
                return None
            if m.status in ("pending", "running"):
                m.status = "cancelled"
                await db.commit()
                await db.refresh(m)
            return _reminder_row(m)
