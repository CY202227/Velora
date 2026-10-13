"""Paired answer replay with frozen retrieval, public snapshots and unchanged scoring."""
import argparse
import asyncio
import hashlib
import json
import logging
import random
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old_answers", type=Path)
    parser.add_argument("old_snapshot", type=Path)
    parser.add_argument("new_answers", type=Path)
    parser.add_argument("new_snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 2:
        raise ValueError("Paired comparison requires at least two repeats")
    logging.disable(logging.CRITICAL)
    from scripts.memory_eval_runtime import saved_connection
    from atom_memory.config import settings
    from atom_memory.models import Atom, AtomKind
    from atom_memory.recall.render import render_detail_block, memory_content_cost
    from atom_memory.eval.locomo import answer_from_context, score_generated_answer, _ANSWER_SYSTEM
    from atom_memory.llm.base import LLMError
    from atom_memory.llm.openai_compat import OpenAICompatLLM

    connection = await saved_connection()
    files = [args.old_answers, args.old_snapshot, args.new_answers, args.new_snapshot]
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    old, old_snapshot, new, new_snapshot = documents
    if [(row["question"], row["answer"]) for row in old["questions"]] != [(row["question"], row["answer"]) for row in new["questions"]]:
        raise ValueError("The two answer reports must contain identical ordered questions and gold labels")
    contexts = {}
    for label, answers, snapshot in (("old", old, old_snapshot), ("new", new, new_snapshot)):
        atoms = {raw["key"]: Atom(space_id=1, key=raw["key"], kind=AtomKind(raw.get("kind") or "event"),
            statement=raw["statement"], detail=raw["detail"],
            happened_on=date.fromisoformat(raw["happened_on"]) if raw.get("happened_on") else None)
            for raw in snapshot["diagnostics"][0]["atoms"]}
        for index, row in enumerate(answers["questions"]):
            contexts[label + "_legacy", index] = (row["context_block"], row["chars_used"])
            retained, size = [], 0
            for key in row["recalled_keys"]:
                atom = atoms[key]
                cost = memory_content_cost(atom, detail="full")
                if retained and size + cost > 1200:
                    break
                retained.append(atom)
                size += cost
            contexts[label + "_current", index] = (render_detail_block(retained), size)
    report = {"complete": False, "mode": "paired answer replay; frozen retrieved keys; no consolidation or source lookup",
        "configuration": {"inputs": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
            "answer_prompt_sha256": hashlib.sha256(_ANSWER_SYSTEM.encode()).hexdigest(),
            "renderer_sha256": hashlib.sha256(Path(sys.modules[render_detail_block.__module__].__file__).read_bytes()).hexdigest(),
            "model_alias": connection["llm_model"], "temperature": 0, "budget_chars": 1200,
            "repeats": args.repeats, "order_seed": 20261013,
            "limitations": "Frozen prior LLM selections isolate answer/rendering effects; not full retrieval or native Agent validation. Gold labels are used only after model output. Old/new snapshots differ in retained facts. Legacy context content and costs come from preserved reports. Token F1 is unchanged and is not independent truth judgment."},
        "results": [], "summary": {}}
    llm = OpenAICompatLLM(connection["llm_base_url"], connection["llm_api_key"], connection["llm_model"],
        timeout=120, extra_body={**settings.llm_extra_body, "max_tokens": 4096, "temperature": 0})
    def evaluate():
        rng = random.Random(20261013)
        for repeat in range(args.repeats):
            for index, question in enumerate(old["questions"]):
                arms = ["old_legacy", "old_current", "new_legacy", "new_current"]
                rng.shuffle(arms)
                for arm in arms:
                    context, size = contexts[arm, index]
                    error = None
                    try:
                        prediction = answer_from_context(llm, question["question"], context)
                    except LLMError as exc:
                        prediction, error = "error: LLMError", exc.code
                    f1, hit, abstained = score_generated_answer(prediction, question["answer"])
                    report["results"].append({"arm": arm, "repeat": repeat + 1, "question_index": index,
                        "question": question["question"], "answer": question["answer"], "context_block": context,
                        "context_sha256": hashlib.sha256(context.encode()).hexdigest(), "chars_used": size,
                        "prediction": prediction, "f1": f1, "hit": hit, "abstained": abstained, "error": error})
                for arm in arms:
                    rows = [row for row in report["results"] if row["arm"] == arm]
                    report["summary"][arm] = {"answers": len(rows), "hits": sum(row["hit"] for row in rows),
                        "mean_f1": sum(row["f1"] for row in rows) / len(rows),
                        "errors": sum(row["error"] is not None for row in rows)}
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print({"repeat": repeat + 1, "question": index + 1}, flush=True)
        report["complete"] = True
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report["summary"], flush=True)
    try:
        await asyncio.to_thread(evaluate)
    finally:
        llm.close()


if __name__ == "__main__":
    asyncio.run(main())
