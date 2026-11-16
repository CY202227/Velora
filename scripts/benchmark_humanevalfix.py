"""Agent adaptation of official HumanEvalFix Python tasks; not a leaderboard submission."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import random
import subprocess
import platform
import os
from collections import Counter
from time import perf_counter
from uuid import uuid4

import httpx

from server.config import Settings
from server.core.execution.service import execute
from server.core.provider.openai_compat import OpenAICompatProvider


async def main(count=10, seed=42, seconds=180, workers=1):
    run_started = perf_counter()
    source = Path("refs/octopack/evaluation/create/humaneval-x/data/python/data/humanevalpack.jsonl")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    if not 1 <= count <= len(rows):
        raise ValueError("sample count outside dataset")
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    selected = random.Random(seed).sample(rows, count)
    commit = subprocess.check_output(["git", "-C", "refs/octopack", "rev-parse", "HEAD"], text=True).strip()
    root = (Path(".runtime-benchmarks") / ("humanevalfix-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6])).resolve()
    root.mkdir(parents=True)
    base = Settings()
    capacity = await OpenAICompatProvider(base.llm_base_url, base.llm_api_key).context_window(base.llm_model)
    if capacity is None:
        raise RuntimeError("model context metadata unavailable")
    settings = base.model_copy(update={
        "database_url": "sqlite+aiosqlite:///" + (root / "runtime.db").as_posix(), "control_token_path": str(root / "control.token"),
        "workspaces_dir": str(root / "workspaces"), "execution_snapshots_dir": str(root / "checkpoints"),
        "execution_output_dir": str(root / "output"), "execution_receipts_dir": str(root / "receipts"),
        "execution_policy_dir": str(root / "policies"), "runtime_archive_dir": str(root / "archives"), "skills_dir": str(root / "skills"),
        "start_memory_sidecar": True, "memory_required": True, "atom_memory_base_url": "http://127.0.0.1:8020",
        "atom_memory_database_url": "sqlite:///" + (root / "memory.db").as_posix(), "atom_memory_sidecar_log": str(root / "memory.log"),
        "memory_space_uid": "benchmark-start-" + uuid4().hex, "computer_enabled": True,
        "execution_location": "local", "execution_backend": "native", "execution_allow_host": False,
        "execution_allow_public_network": False, "execution_read_allowlist": [], "execution_require_approval": False,
        "execution_snapshots_enabled": True, "api_auth_enabled": True, "mcp_enabled": False, "xwiki_enabled": False,
        "web_search_enabled": False, "tts_enabled": False, "reminders_enabled": False,
        "start_local_llm_sidecar": False, "local_llm_enabled": False, "consolidate_every_n_turns": 0,
        "agent_context_window": capacity, "agent_max_steps": None, "agent_run_timeout_seconds": None, "agent_output_reserve": 4096,
    })
    from server import main as application
    application.get_settings = lambda: settings
    for logger in ("httpx", "httpcore", "velora"):
        logging.getLogger(logger).setLevel(logging.WARNING)
    report = {"benchmark": "HumanEvalFix Python, agent adaptation", "official_leaderboard_comparable": False,
        "repository": "https://github.com/bigcode-project/octopack", "commit": commit,
        "dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "seed": seed,
        "selected_ids": [row["task_id"] for row in selected], "attempts_per_task": 1,
        "task_deadline_seconds": seconds, "hidden_tests_exposed_to_agent": False,
        "model": settings.llm_model, "context_window": capacity, "root": str(root), "results": []}
    report["workers"] = workers
    report.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
    report["runtime_source_hashes"] = {str(p).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in sorted(Path("server").rglob("*.py"))}
    report.update(adapter="Velora /api/chat/stream with native execution", platform=platform.platform(),
                  exposed_inputs="official prompt, buggy solution and public examples; no canonical solution or full test",
                  score_definition="one agent attempt per task; internal tool iterations allowed; completed and hidden tests passed",
                  harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    report_path = root / "report.json"
    def save():
        report["resolved"] = sum(row.get("resolved", False) for row in report["results"])
        report["evaluated"] = len(report["results"])
        report["resolved_rate"] = report["resolved"] / len(selected)
        pending = report_path.with_suffix(".tmp")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(pending, report_path)
    save()
    app = application.create_app()
    async with app.router.lifespan_context(app):
        state = app.state.velora
        report["memory_available"] = settings.memory_space_uid in state.memory._ready
        report["memory_started_by_velora"] = state.memory_sidecar.started_by_us
        from server.core.nodes.llm import LLMNode
        runner = next(node for node in state.router.executor.nodes if isinstance(node, LLMNode))
        original_stream = runner._stream_round
        timings = {}
        async def measured_stream(ctx, provider, messages, *, tools):
            started = perf_counter()
            item = {"step": ctx.agent_run.step}
            try:
                result = await original_stream(ctx, provider, messages, tools=tools)
                item["finish_reason"] = result.finish_reason
                return result
            except BaseException as exc:
                item["error_type"] = type(exc).__name__
                raise
            finally:
                item["seconds"] = round(perf_counter() - started, 3)
                timings.setdefault(ctx.session_id, []).append(item)
        runner._stream_round = measured_stream
        async def grade(code, tests):
            # Grading runs in a different native workspace, after the agent has finished.
            grader_id = "grader-" + uuid4().hex
            code = code + "\n\n" + tests + "\nprint('OFFICIAL_TESTS_PASSED')\n"
            result = json.loads(await execute(settings, grader_id, "run_python", {"code": code}, execution_id=uuid4().hex,
                store=state.router.execution_store))
            return {"passed": result.get("ok") is True and result.get("exit_code") == 0 and "OFFICIAL_TESTS_PASSED" in result.get("stdout", ""),
                    "exit_code": result.get("exit_code"), "reason": result.get("reason"), "stderr": result.get("stderr", "")[-2000:]}
        async def capture_loop(result, identifier):
            if not identifier:
                return
            if not result.get("run_id"):
                tasks = await state.tasks.recent(identifier)
                if tasks:
                    task = await state.tasks.read(tasks[0]["task_id"])
                    terminal = (task.get("result") or {}).get("result", {})
                    result.update(agent_status=terminal.get("status", task["status"]),
                                  agent_reason=terminal.get("reason"), run_id=terminal.get("run_id"), steps=terminal.get("step", 0))
            checkpoint = await state.router.run_store.read(result["run_id"]) if result.get("run_id") else None
            if not checkpoint:
                return
            run = checkpoint["run"]
            result.update(input_tokens=run["input_tokens"], output_tokens=run["output_tokens"], tool_calls=len(run["tool_executions"]))
            result["loop"] = {key: run.get(key, 0) for key in ("retries", "forced_continuations", "output_recoveries", "context_recoveries", "compactions", "compaction_attempts", "pruned_results")}
            commands = Counter((t["name"], t["source_digest"]) for t in run["tool_executions"] if t.get("source_digest"))
            result["loop"].update(tool_errors=sum(bool(t.get("is_error")) for t in run["tool_executions"]),
                unknown_results=sum(t["status"] in ("running", "result_unknown") for t in run["tool_executions"]),
                repeated_commands=sum(n-1 for n in commands.values()), max_same_command=max(commands.values(), default=0),
                covered_file_changes=sum(t.get("workspace_changed") is True for t in run["tool_executions"]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://benchmark",
                                    headers={"X-Velora-Token": state.control_token}, timeout=seconds + 30) as client:
            async def run_case(row):
                started = perf_counter()
                result = {"task_id": row["task_id"], "bug_type": row["bug_type"], "resolved": False}
                identifier = None
                agent_started = None
                try:
                    canonical = await grade(row["prompt"] + row["canonical_solution"], row["test"])
                    if not canonical["passed"]:
                        raise RuntimeError("official canonical solution failed environment preflight: " + json.dumps(canonical))
                    original = await grade(row["prompt"] + row["buggy_solution"], row["test"])
                    result["baseline_tests_failed"] = not original["passed"]
                    result["control_seconds"] = round(perf_counter() - started, 3)
                    memory_uid = "benchmark-task-" + uuid4().hex
                    session = await state.store.create_session(persona_id=settings.default_persona_id,
                        memory_space_uid=memory_uid, model=settings.llm_model, tts_enabled=False, style_knobs={"warmth": 0})
                    identifier = session.id
                    await state.memory.ensure_space(memory_uid)
                    workspace = root / "workspaces" / identifier
                    workspace.mkdir(parents=True)
                    (workspace / "solution.py").write_text(row["prompt"] + row["buggy_solution"], encoding="utf-8")
                    (workspace / "example_tests.py").write_text("from solution import " + row["entry_point"] + "\n" + row["example_test"], encoding="utf-8")
                    example_digest = hashlib.sha256((workspace / "example_tests.py").read_bytes()).hexdigest()
                    text = ("修复当前工作目录 solution.py 中的错误函数。保持函数签名和接口，依据函数内文档与示例实现正确行为。"
                            "可以用现有工具读取、编辑并运行自己的检查；example_tests.py 仅含公开示例。不得修改示例测试。"
                            "最终请把完整修复留在 solution.py，不要只在回复中贴代码。隐藏测试不在你的工作目录中。完成后简短总结。")
                    agent_started = perf_counter()
                    response = await asyncio.wait_for(client.post("/api/chat/stream", json={"session_id": identifier, "text": text}), seconds)
                    result["agent_seconds"] = round(perf_counter() - agent_started, 3)
                    response.raise_for_status()
                    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                    (root / (row["task_id"].replace("/", "-") + "-events.json")).write_text(json.dumps(frames, ensure_ascii=False), encoding="utf-8")
                    endings = [frame for frame in frames if isinstance(frame.get("result"), dict) and "status" in frame["result"]]
                    if not endings:
                        raise RuntimeError("agent did not return a terminal outcome")
                    terminal = endings[-1]["result"]
                    result.update(agent_status=terminal["status"], agent_reason=terminal.get("reason"), steps=terminal["step"], run_id=terminal.get("run_id"))
                    contents = state.router.snapshots._read(state.router.snapshots._root(workspace), "solution.py")
                    if contents is None:
                        raise RuntimeError("solution file missing")
                    grading = await grade(contents[0].decode("utf-8-sig"), row["test"])
                    result["grading"] = grading
                    result["example_tests_unchanged"] = hashlib.sha256((workspace / "example_tests.py").read_bytes()).hexdigest() == example_digest
                    result["resolved"] = grading["passed"] and terminal["status"] == "completed"
                except Exception as exc:
                    result.update(error_type=type(exc).__name__, error=str(exc)[:1200])
                    if agent_started is not None:
                        result.setdefault("agent_seconds", round(perf_counter() - agent_started, 3))
                    if identifier:
                        for task_id in list(state.tasks.active):
                            snapshot = await state.tasks.read(task_id)
                            if snapshot["session_id"] == identifier:
                                task = state.tasks.active[task_id]
                                state.tasks.cancel(task_id)
                                await asyncio.wait_for(asyncio.shield(task), 30)
                await capture_loop(result, identifier)
                result["seconds"] = round(perf_counter() - started, 2)
                result["model_rounds"] = timings.pop(identifier, [])
                report["results"].append(result)
                save()
                print(json.dumps({k: result[k] for k in ("task_id", "resolved", "seconds", "steps", "error_type") if k in result}), flush=True)
            queue = asyncio.Queue()
            for row in selected:
                queue.put_nowait(row)
            async def worker():
                while not queue.empty():
                    row = queue.get_nowait()
                    await run_case(row)
            await asyncio.gather(*(worker() for _ in range(workers)))
    save()
    report.update(status="completed", finished_at=datetime.now(timezone.utc).isoformat(), wall_seconds=round(perf_counter() - run_started, 2))
    save()
    print(json.dumps({"report": str(report_path), "resolved": report["resolved"], "total": len(selected), "rate": report["resolved_rate"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(main(args.count, args.seed, args.seconds, args.workers))
