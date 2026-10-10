"""Session / turn / app_settings persistence (SQLite or MySQL via SQLAlchemy)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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


@dataclass
class PersonaRow:
    id: str
    name: str
    system_prompt: str
    opener: str | None
    begin_dialogs: list[str]
    style_defaults: dict
    tool_names: list[str] | None
    skill_names: list[str] | None
    source: str
    description: str
    personality: str
    scenario: str
    post_history_instructions: str
    alternate_greetings: list[str]
    character_book: dict | None
    avatar_path: str | None
    avatar_url: str | None
    import_meta: dict | None
    created_at: str
    updated_at: str


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


class MemoryDeliveryModel(Base):
    __tablename__ = "memory_deliveries"
    last_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    space_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class AppSettingModel(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class PersonaModel(Base):
    __tablename__ = "personas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    opener: Mapped[str | None] = mapped_column(Text, nullable=True)
    begin_dialogs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    style_defaults_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tool_names_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_names_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    personality: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scenario: Mapped[str] = mapped_column(Text, nullable=False, default="")
    post_history_instructions: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    alternate_greetings_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    character_book_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    import_meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


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


def _json_list(raw: str | None) -> list:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _json_dict(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _persona_row(m: PersonaModel) -> PersonaRow:
    tool_raw = m.tool_names_json
    skill_raw = m.skill_names_json
    tool_names = None
    skill_names = None
    if tool_raw is not None:
        tool_names = [str(x) for x in _json_list(tool_raw)]
    if skill_raw is not None:
        skill_names = [str(x) for x in _json_list(skill_raw)]
    book: dict | None = None
    if m.character_book_json and m.character_book_json.strip() not in (
        "",
        "null",
    ):
        try:
            parsed = json.loads(m.character_book_json)
        except json.JSONDecodeError:
            parsed = None
        book = parsed if isinstance(parsed, dict) else None
    meta = None
    if m.import_meta_json:
        meta = _json_dict(m.import_meta_json) or None
    return PersonaRow(
        id=m.id,
        name=m.name,
        system_prompt=m.system_prompt or "",
        opener=m.opener,
        begin_dialogs=[str(x) for x in _json_list(m.begin_dialogs_json)],
        style_defaults=_json_dict(m.style_defaults_json),
        tool_names=tool_names,
        skill_names=skill_names,
        source=m.source or "custom",
        description=m.description or "",
        personality=m.personality or "",
        scenario=m.scenario or "",
        post_history_instructions=m.post_history_instructions or "",
        alternate_greetings=[
            str(x) for x in _json_list(m.alternate_greetings_json)
        ],
        character_book=book,
        avatar_path=m.avatar_path,
        avatar_url=m.avatar_url,
        import_meta=meta,
        created_at=_iso(m.created_at),
        updated_at=_iso(m.updated_at),
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
        persona_id: str | None = None,
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
            if persona_id is not None:
                m.persona_id = persona_id
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

    async def save_exchange(
        self,
        session_id: str,
        user_text: str | None,
        assistant_text: str,
        *,
        source: str = "chat",
        attachments: list[dict] | None = None,
        memory_uid: str | None = None,
        memory_payload: dict | None = None,
    ) -> tuple[TurnRow, dict | None]:
        """Commit the complete exchange and its memory delivery together."""
        now = _utcnow()
        user_id = str(uuid.uuid4()) if user_text is not None else None
        assistant_id = str(uuid.uuid4())
        payload = None
        if memory_payload is not None:
            if memory_uid is None:
                raise ValueError("memory_uid is required for delivery")
            payload = dict(memory_payload)
            payload["idempotency_key"] = assistant_id
            payload["external_ref"] = {
                "system": "velora", "session_id": session_id,
                "turn_id": user_id if payload["kind"] == "correction" and user_id else assistant_id,
            }
        assistant = TurnModel(
            id=assistant_id, session_id=session_id, role="assistant",
            content=assistant_text, created_at=now + timedelta(microseconds=1), source=source,
            attachments=json.dumps(attachments or [], ensure_ascii=False),
        )
        async with self._session_factory() as db:
            if user_id is not None:
                db.add(TurnModel(id=user_id, session_id=session_id, role="user",
                                 content=user_text, created_at=now, source=source, attachments="[]"))
                # Preserve user-before-assistant insertion order on equal timestamps.
                await db.flush()
            db.add(assistant)
            if payload is not None:
                db.add(MemoryDeliveryModel(id=assistant_id, space_uid=memory_uid,
                                           payload=json.dumps(payload, ensure_ascii=False)))
            session = await db.get(SessionModel, session_id)
            if session is None:
                raise ValueError("session not found")
            session.updated_at = now
            await db.commit()
            return _turn_row(assistant), payload

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

    def _persona_to_model_fields(self, p: "Persona") -> dict:
        from server.core.persona.default import Persona as PersonaT

        del PersonaT  # type hint only
        return {
            "name": p.name,
            "system_prompt": p.system_prompt or "",
            "opener": p.opener,
            "begin_dialogs_json": json.dumps(
                p.begin_dialogs or [], ensure_ascii=False
            ),
            "style_defaults_json": json.dumps(
                p.style_defaults or {}, ensure_ascii=False
            ),
            "tool_names_json": (
                None
                if p.tool_names is None
                else json.dumps(p.tool_names, ensure_ascii=False)
            ),
            "skill_names_json": (
                None
                if p.skill_names is None
                else json.dumps(p.skill_names, ensure_ascii=False)
            ),
            "source": p.source or "custom",
            "description": p.description or "",
            "personality": p.personality or "",
            "scenario": p.scenario or "",
            "post_history_instructions": p.post_history_instructions or "",
            "alternate_greetings_json": json.dumps(
                p.alternate_greetings or [], ensure_ascii=False
            ),
            "character_book_json": (
                json.dumps(p.character_book, ensure_ascii=False)
                if p.character_book is not None
                else None
            ),
            "avatar_path": p.avatar_path,
            "avatar_url": p.avatar_url,
            "import_meta_json": (
                json.dumps(p.import_meta, ensure_ascii=False)
                if p.import_meta is not None
                else None
            ),
        }

    async def list_personas(self) -> list[PersonaRow]:
        async with self._session_factory() as db:
            result = await db.scalars(
                select(PersonaModel).order_by(PersonaModel.updated_at.desc())
            )
            return [_persona_row(m) for m in result.all()]

    async def get_persona(self, persona_id: str) -> PersonaRow | None:
        async with self._session_factory() as db:
            m = await db.get(PersonaModel, persona_id)
            return _persona_row(m) if m else None

    async def upsert_persona(self, persona: "Persona") -> PersonaRow:
        from server.core.persona.default import Persona as PersonaCls

        if not isinstance(persona, PersonaCls):
            raise TypeError("persona required")
        fields = self._persona_to_model_fields(persona)
        now = _utcnow()
        async with self._session_factory() as db:
            m = await db.get(PersonaModel, persona.id)
            if m is None:
                m = PersonaModel(id=persona.id, created_at=now, updated_at=now, **fields)
                db.add(m)
            else:
                for k, v in fields.items():
                    setattr(m, k, v)
                m.updated_at = now
            await db.commit()
            await db.refresh(m)
            return _persona_row(m)

    async def delete_persona(self, persona_id: str) -> bool:
        async with self._session_factory() as db:
            m = await db.get(PersonaModel, persona_id)
            if m is None:
                return False
            await db.delete(m)
            await db.commit()
            return True


    async def save_memory_delivery(self, key: str, uid: str, payload: dict) -> None:
        async with self._session_factory() as db:
            previous = await db.get(MemoryDeliveryModel, key)
            if previous is not None:
                if previous.space_uid != uid or json.loads(previous.payload) != payload:
                    raise ValueError("delivery key reused with a different payload")
                return
            db.add(MemoryDeliveryModel(id=key, space_uid=uid, payload=json.dumps(payload, ensure_ascii=False)))
            await db.commit()

    async def pending_memory_deliveries(self, limit: int = 20) -> list[tuple[str, str, dict]]:
        async with self._session_factory() as db:
            rows = (await db.execute(select(MemoryDeliveryModel).order_by(MemoryDeliveryModel.last_attempt, MemoryDeliveryModel.id).limit(limit))).scalars().all()
            return [(row.id, row.space_uid, json.loads(row.payload)) for row in rows]

    async def finish_memory_delivery(self, key: str) -> None:
        async with self._session_factory() as db:
            row = await db.get(MemoryDeliveryModel, key)
            if row is not None:
                await db.delete(row)
                await db.commit()


    async def touch_memory_delivery(self, key: str) -> None:
        async with self._session_factory() as db:
            row = await db.get(MemoryDeliveryModel, key)
            if row is not None:
                row.last_attempt = _utcnow()
                await db.commit()
