"""Exercise Velora's real memory nodes, streaming provider and tool loop on public data."""
import asyncio
import hashlib
import json
import logging
import sys
import argparse
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--normalize-answer-spans", action="store_true")
    parser.add_argument("--temperature", type=float)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Never print private endpoint addresses from provider exceptions.
    from scripts.memory_eval_runtime import restore_public_memory, saved_connection
    from atom_memory.config import settings as memory_settings
    from atom_memory.llm.openai_compat import OpenAICompatLLM
    from atom_memory.eval.locomo import score_generated_answer
    from atom_memory.eval.answer_span import extract_answer_span
    from server.config import Settings
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest
    from server.core.memory.client import AtomMemoryClient
    from server.core.nodes.recall_memory import RecallMemoryNode
    from server.core.nodes.compose_prompt import ComposePromptNode
    from server.core.nodes.llm import LLMNode
    from server.core.persona.default import DEFAULT_PERSONA
    from server.core.provider.openai_compat import OpenAICompatProvider
    from server.core.tools.builtin import register_builtin_tools
    from server.core.tools.registry import ToolRegistry
    import httpx
    dataset, snapshot, output = args.dataset, args.snapshot, args.output
    connection = await saved_connection()
    extra = {**memory_settings.llm_extra_body, "max_tokens": 4096}
    if args.temperature is not None:
        extra["temperature"] = args.temperature
    llm = OpenAICompatLLM(connection["llm_base_url"], connection["llm_api_key"], connection["llm_model"],
        timeout=120, extra_body=extra)
    runtime = restore_public_memory(dataset, snapshot, llm)

    class MemoryClient(AtomMemoryClient):
        async def call(self, endpoint, payload):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app), base_url="http://public-memory") as client:
                response = await client.post(f"/spaces/{runtime.uid}/{endpoint}", json=payload)
                if response.status_code >= 400:
                    return {"error": "memory request failed", "http_status": response.status_code, "hits": []}
                return response.json()
        async def recall(self, uid, query, **kwargs):
            return await self.call("recall", {"query": query, **kwargs})
        async def search_sources(self, uid, query, *, budget_chars=2400, method="bm25"):
            return await self.call("sources/search", {"query": query, "max_sources": 4, "budget_chars": budget_chars, "method": method})
        async def expand(self, uid, keys, *, budget_chars=1200):
            return await self.call("atoms/expand", {"keys": keys, "budget_chars": budget_chars, "with_evidence": True})

    memory = MemoryClient("http://public-memory")
    tools = ToolRegistry()
    register_builtin_tools(tools)
    # Evaluation only: retain the real persona instructions, but use question language and memory-only tools.
    persona = replace(DEFAULT_PERSONA, begin_dialogs=[], tool_names=["memory_search", "memory_expand"],
        system_prompt=DEFAULT_PERSONA.system_prompt + "\n本轮是记忆问答，请使用提问语言简短回答。若查证后仍不知道，回答 unknown。")
    class EvaluationProvider(OpenAICompatProvider):
        def _maybe_sampling(self, payload):
            super()._maybe_sampling(payload)
            if args.temperature is not None:
                payload["temperature"] = args.temperature
    provider = EvaluationProvider(connection["llm_base_url"], connection["llm_api_key"], timeout=120)
    results = []
    report = {"mode": "real Velora RecallMemory -> ComposePrompt -> LLMNode streaming/tool loop; saved public atoms",
        "complete": False, "configuration": {"dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "model_alias": connection["llm_model"], "initial_budget": 400, "initial_max_atoms": 5, "max_tool_rounds": 4,
            "answer_span_normalization": "--normalize-answer-spans" in sys.argv,
            "temperature": args.temperature,
            "limitation": "Public snapshot replay, no new ingestion/consolidation or persistence. Evaluation persona uses question language and two read-only memory tools. Missing snapshot kind defaults to event; all atoms are L1. Raw and optional normalized-span token scores are separate; normalizer sees no gold and must copy an asserted span. Neither is independent truth/grounding judgment."}, "questions": results}
    try:
        for index, question in enumerate(runtime.sample.qa, 1):
            events = []
            async def emit(event):
                if event.type == "tool_call":
                    events.append({"name": event.data.get("name"), "arguments": event.data.get("arguments"), "round": event.data.get("round")})
            ctx = TurnContext(request=TurnRequest("public-eval", question.question), session_id="public-eval", persona=persona,
                memory_space_uid=runtime.uid, model=connection["llm_model"], llm_base_url=connection["llm_base_url"], llm_api_key=connection["llm_api_key"], emit=emit)
            ctx.extras.update(memory=memory, settings=Settings())
            await RecallMemoryNode(memory).process(ctx)
            await ComposePromptNode(tools).process(ctx)
            await LLMNode(provider, tools=tools, max_tool_rounds=4).process(ctx)
            prediction = ctx.assistant_text or f"error: {ctx.stop_reason}"
            f1, hit, abstained = score_generated_answer(prediction, question.answer)
            tool_results = []
            for message in ctx.messages:
                if message.get("role") == "tool":
                    try:
                        data = json.loads(message["content"])
                        tool_results.append({"ok": data.get("ok"), "partial": data.get("partial"), "fallback_from": data.get("fallback_from"),
                            "source_coverage": data.get("source_evidence", {}).get("coverage"), "errors": data.get("errors")})
                    except (ValueError, TypeError):
                        tool_results.append({"error": "unparseable tool result"})
            results.append({"question": question.question, "answer": question.answer, "prediction": prediction, "f1": f1, "hit": hit,
                "category": question.category, "initial_memory_block": ctx.memory_block,
                "stop_reason": ctx.stop_reason, "tool_calls": events, "tool_results": tool_results, "abstained": abstained})
            if "--normalize-answer-spans" in sys.argv:
                try:
                    span = await asyncio.to_thread(extract_answer_span, llm, question.question, prediction)
                    span_f1, span_hit, _ = score_generated_answer(span, question.answer)
                    results[-1].update(answer_span=span, span_f1=span_f1, span_hit=span_hit)
                except Exception as exc:
                    results[-1].update(answer_span_error=type(exc).__name__, span_f1=0, span_hit=False)
            report["summary"] = {"total": len(results), "hits": sum(row["hit"] for row in results), "mean_f1": sum(row["f1"] for row in results) / len(results),
                "questions_with_tools": sum(bool(row["tool_calls"]) for row in results)}
            if "--normalize-answer-spans" in sys.argv:
                report["summary"].update(span_hits=sum(row["span_hit"] for row in results), span_mean_f1=sum(row["span_f1"] for row in results) / len(results))
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print({"question": index, "hit": hit, "tool_names": [event["name"] for event in events], "stop_reason": ctx.stop_reason}, flush=True)
        report["complete"] = True
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        runtime.close()
        llm.close()


if __name__ == "__main__":
    asyncio.run(main())
