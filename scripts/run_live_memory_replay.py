"""Replay public atom snapshots with original evidence links and repeated QA."""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', type=Path)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--method', choices=['bm25', 'llm'], default='bm25')
    parser.add_argument('--detail', choices=['statement', 'full'], default='statement')
    parser.add_argument('--atom-only', action='store_true')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--temperature', type=float)
    args = parser.parse_args()
    if args.repeats < 1:
        raise ValueError('repeats must be positive')
    logging.disable(logging.CRITICAL)
    from scripts.memory_eval_runtime import saved_connection, restore_public_memory
    from atom_memory.config import settings
    from atom_memory.eval.locomo import evaluate_locomo_sample, aggregate_reports, _ANSWER_SYSTEM
    from atom_memory.llm.openai_compat import OpenAICompatLLM
    connection = await saved_connection()
    extra = {**settings.llm_extra_body, 'max_tokens': 4096}
    if args.temperature is not None:
        extra['temperature'] = args.temperature
    llm = OpenAICompatLLM(connection['llm_base_url'], connection['llm_api_key'], connection['llm_model'], timeout=120, extra_body=extra)
    runtime = restore_public_memory(args.dataset, args.snapshot, llm)
    configuration = {'answer_prompt_sha256': hashlib.sha256(_ANSWER_SYSTEM.encode()).hexdigest(),
        'snapshot_sha256': hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
        'dataset_sha256': hashlib.sha256(args.dataset.read_bytes()).hexdigest(), 'method': args.method,
        'detail': args.detail, 'atom_only': args.atom_only, 'atom_budget': 1200,
        'source_budget': 0 if args.atom_only else 2400, 'max_atoms': 10, 'max_sources': 0 if args.atom_only else 4,
        'model_alias': connection['llm_model'], 'source_turns': 6, 'repeats': args.repeats, 'temperature': args.temperature,
        'limitation': 'Saved public atoms, no new consolidation. Evidence links are restored only when explicitly present in the snapshot and the exact original subset matches. Each repeat makes new retrieval and QA model calls. Repeated questions are not independent new test cases. Token scores are not independent truth judgment or a full Velora tool-use evaluation.'}
    reports = []
    def evaluate():
        args.output.write_text(json.dumps({'complete': False, 'configuration': configuration}, indent=2), encoding='utf-8')
        for repeat in range(args.repeats):
            report = evaluate_locomo_sample(runtime.client, runtime.uid, runtime.sample, method=args.method, max_atoms=10,
                budget_chars=1200, detail=args.detail, score_mode='qa', llm=llm, source_evidence=not args.atom_only)
            reports.append(report)
            payload = aggregate_reports(reports, mode='snapshot replay; repeated retrieval and QA' + ('; atom only' if args.atom_only else '; historical-source lookup')).to_dict()
            payload['questions'] = [{**asdict(result), 'repeat': number} for number, item in enumerate(reports, 1) for result in item.results]
            payload['configuration'] = configuration
            payload['complete'] = repeat + 1 == args.repeats
            payload['repeat_scores'] = [{'hits': item.hits, 'mean_f1': item.mean_f1, 'total': item.total} for item in reports]
            payload['llm_diagnostics'] = llm.diagnostics
            args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            print({'repeat': repeat + 1, 'hits': report.hits, 'mean_f1': report.mean_f1, 'avg_chars': report.avg_chars_used}, flush=True)
    try:
        await asyncio.to_thread(evaluate)
    finally:
        runtime.close()
        llm.close()


if __name__ == '__main__':
    asyncio.run(main())
