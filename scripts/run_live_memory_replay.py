"""Replay a public atom snapshot with retained-source lookup; no new consolidation."""
import asyncio
import hashlib
import json
import sys
import argparse
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--method", choices=["bm25", "llm"], default="bm25")
    parser.add_argument("--detail", choices=["statement", "full"], default="statement")
    parser.add_argument("--atom-only", action="store_true")
    args = parser.parse_args()
    from server.config import Settings
    from server.core.conversation.store import ConversationStore
    store = ConversationStore(Settings().database_url)
    try:
        connection = {key: await store.get_setting(key) for key in ("llm_base_url", "llm_api_key", "llm_model")}
    finally:
        await store.close()
    dataset_path, snapshot_path, out_path = args.dataset, args.snapshot, args.output
    from atom_memory.config import settings
    from atom_memory.db import get_session
    from atom_memory.eval.locomo import load_locomo, ingest_locomo_sample, evaluate_locomo_sample, aggregate_reports, _ANSWER_SYSTEM
    from atom_memory.eval.run_locomo import _build_client
    from atom_memory.llm.openai_compat import OpenAICompatLLM
    from atom_memory.main import app
    from atom_memory.models import Atom, AtomKind, Source, SourceStatus
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))["diagnostics"][0]
    sample = load_locomo(dataset_path)[0]
    llm = OpenAICompatLLM(connection["llm_base_url"], connection["llm_api_key"], connection["llm_model"],
        timeout=120, extra_body={**settings.llm_extra_body, "max_tokens": 4096})
    def evaluate():
        client = _build_client(llm)
        try:
            space = client.post("/spaces", json={"owner_id": "public-replay", "subject_id": sample.sample_id}).json()
            ingest_locomo_sample(client, space["uid"], sample, salience=0.2, source_turns=6)
            generator = app.dependency_overrides[get_session]()
            session = next(generator)
            try:
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
            finally:
                generator.close()
            report = evaluate_locomo_sample(client, space["uid"], sample, method=args.method, max_atoms=10,
                budget_chars=1200, detail=args.detail, score_mode="qa", llm=llm, source_evidence=not args.atom_only)
            payload = aggregate_reports([report], mode="snapshot replay; QA; atom recall" + ("" if args.atom_only else " plus historical-source lookup")).to_dict()
            payload["questions"] = [asdict(result) for result in report.results]
            payload["configuration"] = {"answer_prompt_sha256": hashlib.sha256(_ANSWER_SYSTEM.encode()).hexdigest(), "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(), "method": args.method,
                "detail": args.detail, "atom_only": args.atom_only,
                "atom_budget": 1200, "source_budget": 0 if args.atom_only else 2400, "max_atoms": 10, "max_sources": 0 if args.atom_only else 4,
                "model_alias": connection["llm_model"], "source_turns": 6,
                "limitation": "Saved atoms and questions, no new consolidation. Old snapshots without kind default to event (all saved atoms are L1). Atom-only mode uses no raw sources; otherwise automatic lookup adds evidence budget. Not a full Velora tool-use evaluation or independent truth judgment."}
            payload["llm_diagnostics"] = llm.diagnostics
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print({key: payload[key] for key in ("total", "hits", "accuracy", "mean_f1", "avg_chars_used")})
        finally:
            client.close()
            app.dependency_overrides.clear()
            llm.close()
    await asyncio.to_thread(evaluate)


if __name__ == "__main__":
    asyncio.run(main())
