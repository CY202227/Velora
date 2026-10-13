"""Run a bounded LoCoMo pilot using Velora's saved model connection."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def main():
    from server.config import Settings
    from server.core.conversation.store import ConversationStore
    store = ConversationStore(Settings().database_url)
    try:
        connection = {key: await store.get_setting(key) for key in ("llm_base_url", "llm_api_key", "llm_model")}
    finally:
        await store.close()
    if not all(connection.values()):
        raise RuntimeError("Configure a model in Velora before live evaluation")
    dataset, out = map(Path, sys.argv[1:3])
    sample = json.loads(dataset.read_text(encoding="utf-8"))[0]
    available, subset = set(), {}
    for key, value in sample["conversation"].items():
        if key.startswith("session_"):
            if int(key.split("_")[1]) <= 3:
                subset[key] = value
                if isinstance(value, list):
                    available.update(str(turn["dia_id"]) for turn in value if turn.get("dia_id"))
        else:
            subset[key] = value
    questions = [q for q in sample["qa"] if q.get("evidence") and set(map(str, q["evidence"])) <= available][:12]
    pilot_path = dataset.parent / "locomo-live-pilot.json"
    pilot_path.write_text(json.dumps([{**sample, "conversation": subset, "qa": questions}]), encoding="utf-8")
    from atom_memory.config import settings
    settings.llm_base_url, settings.llm_api_key, settings.llm_model = (connection[key] for key in ("llm_base_url", "llm_api_key", "llm_model"))
    settings.llm_timeout_seconds = 120
    settings.consolidate_max_sources = 1
    settings.consolidate_extract_facts = "--extract-facts" in sys.argv
    settings.llm_extra_body = {**settings.llm_extra_body, "max_tokens": 4096}
    from atom_memory.eval.run_locomo import main as evaluate
    try:
        arguments = [str(pilot_path), "--consolidate", "llm", "--score", "qa", "--method", "bm25", "--budget-chars", "1200", "--max-atoms", "10", "--out", str(out), "--diagnostics", "--source-turns", "6"]
        if "--diagnose-only" in sys.argv:
            arguments += ["--max-questions", "0"]
        code = await asyncio.to_thread(evaluate, arguments)
        if code:
            raise RuntimeError("live evaluation failed")
        data = json.loads(out.read_text(encoding="utf-8"))
        data["pilot_scope"] = {"sessions": 3, "questions": len(questions), "batch_size": 1, "source_turns": 6, "timeout_seconds": 120, "extract_facts": settings.consolidate_extract_facts, "selection": "first 12 questions whose evidence lies inside first 3 sessions", "limitation": "input granularity differs from earlier whole-session pilot; not comparable to 98-question oracle runs; model alias does not establish model identity"}
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        message = str(exc)
        reason = "consolidation_no_progress" if "no progress" in message else "consolidation_failed" if "consolidate failed" in message else "evaluation_failed"
        import re
        status = re.search(r"request failed: (\d{3})", message)
        subtype = next((name for name in ("JSONDecodeError", "LLMError", "ValidationError", "KeyError", "ValueError") if name in message), None)
        code = re.search(r"LLMError\[([a-z_]+)\]", message)
        safe_code = code.group(1) if code else None
        print({"failure_phase": reason, "nested_error_type": subtype, "error_code": safe_code, "http_status": int(status.group(1)) if status else None})
        out.write_text(json.dumps({"status": "failed", "error_type": type(exc).__name__, "failure_reason": reason, "nested_error_type": subtype, "error_code": safe_code, "http_status": int(status.group(1)) if status else None, "model_alias": connection["llm_model"], "sessions": 3, "questions": len(questions)}, indent=2), encoding="utf-8")
        print("Live pilot failed; no private request data saved in report")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
