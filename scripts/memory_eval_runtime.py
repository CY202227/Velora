"""Isolated public-memory setup shared by live evaluation scripts."""
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


async def saved_connection():
    from server.config import Settings
    from server.core.conversation.store import ConversationStore
    store = ConversationStore(Settings().database_url)
    try:
        result = {key: await store.get_setting(key) for key in ("llm_base_url", "llm_api_key", "llm_model")}
    finally:
        await store.close()
    if not all(result.values()):
        raise RuntimeError("Configure a model in Velora before live evaluation")
    return result


@dataclass
class PublicMemory:
    client: object
    uid: str
    sample: object
    app: object
    engine: object
    original_overrides: dict

    def close(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.app.dependency_overrides.update(self.original_overrides)
        self.engine.dispose()


def restore_public_memory(dataset: Path, snapshot_path: Path, llm) -> PublicMemory:
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from atom_memory.api.deps import get_llm, require_api_key
    from atom_memory.db import get_session
    from atom_memory.eval.locomo import load_locomo, ingest_locomo_sample
    from atom_memory.main import app
    from atom_memory.models import Atom, AtomKind, Source, SourceStatus
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))["diagnostics"][0]
    sample = load_locomo(dataset)[0]
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    def session_dependency():
        with Session(engine) as session:
            yield session
    original = dict(app.dependency_overrides)
    app.dependency_overrides.update({get_session: session_dependency, get_llm: lambda: llm, require_api_key: lambda: None})
    client = TestClient(app)
    try:
        space = client.post("/spaces", json={"owner_id": "public-pilot", "subject_id": sample.sample_id}).json()
        ingest_locomo_sample(client, space["uid"], sample, salience=0.2, source_turns=6)
        with Session(engine) as session:
            for atom in snapshot["atoms"]:
                session.add(Atom(space_id=space["id"], key=atom["key"], kind=AtomKind(atom.get("kind") or "event"),
                    statement=atom["statement"], detail=atom["detail"], memory_layer=atom["memory_layer"],
                    happened_on=date.fromisoformat(atom["happened_on"]) if atom["happened_on"] else None))
            for source in snapshot["source_statuses"]:
                row = session.get(Source, source["id"])
                if row:
                    row.status = SourceStatus(source["status"])
                    session.add(row)
            session.commit()
        return PublicMemory(client, space["uid"], sample, app, engine, original)
    except BaseException:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)
        engine.dispose()
        raise
