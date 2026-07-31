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
    String,
    Text,
    select,
    func,
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


def _turn_row(m: TurnModel) -> TurnRow:
    return TurnRow(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        created_at=_iso(m.created_at),
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

    async def add_turn(self, session_id: str, role: str, content: str) -> TurnRow:
        tid = str(uuid.uuid4())
        now = _utcnow()
        turn = TurnModel(
            id=tid,
            session_id=session_id,
            role=role,
            content=content,
            created_at=now,
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
