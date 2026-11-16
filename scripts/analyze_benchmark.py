"""Summarize observed agent benchmark outcomes without conflating model and runtime failures."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sqlite3


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(report):
    rows = report["results"]
    reasons = Counter()
    for row in rows:
        if row.get("resolved"):
            reasons["resolved"] += 1
        elif row.get("error_type") == "TimeoutError":
            reasons["evaluation_deadline"] += 1
        elif row.get("agent_status") == "completed" and not row.get("grading", {}).get("passed"):
            reasons["completed_but_hidden_tests_failed"] += 1
        elif row.get("agent_reason"):
            reasons["runtime_stop:" + row["agent_reason"]] += 1
        else:
            reasons["evaluation_or_environment_error:" + row.get("error_type", "unknown")] += 1
    successful_times = [r["agent_seconds"] for r in rows if r.get("resolved") and "agent_seconds" in r]
    all_times = [r["agent_seconds"] for r in rows if "agent_seconds" in r]
    loop_totals = Counter()
    for row in rows:
        loop_totals.update({key: value for key, value in row.get("loop", {}).items() if key != "max_same_command"})
    controls = [r for r in rows if r.get("baseline_tests_failed") is True]
    model_seconds = sum(round_["seconds"] for row in rows for round_ in row.get("model_rounds", []))
    runtime_unchanged = all(Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                            for path, digest in report.get("runtime_source_hashes", {}).items())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "benchmark": report["benchmark"], "model": report["model"],
        "run_status": report.get("status", "unknown"), "planned": len(report["selected_ids"]), "evaluated": len(rows),
        "resolved": sum(r.get("resolved", False) for r in rows),
        "resolved_rate_of_evaluated": sum(r.get("resolved", False) for r in rows) / len(rows) if rows else None,
        "failure_categories": dict(reasons), "reference_controls": {"initially_failing": len(controls),
            "resolved_initially_failing": sum(r.get("resolved", False) for r in controls),
            "already_passing_originals": sum(r.get("baseline_tests_failed") is False for r in rows)},
        "agent_seconds": {"mean": statistics.mean(all_times) if all_times else None, "p50": percentile(all_times, .5),
            "p95": percentile(all_times, .95), "max": max(all_times, default=None),
            "successful_mean": statistics.mean(successful_times) if successful_times else None},
        "mean_steps": statistics.mean([r["steps"] for r in rows if "steps" in r]) if any("steps" in r for r in rows) else None,
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows), "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "tool_calls": sum(r.get("tool_calls", 0) for r in rows), "loop_totals": dict(loop_totals),
        "tasks_with_tool_errors": sum(r.get("loop", {}).get("tool_errors", 0) > 0 for r in rows),
        "resolved_despite_tool_errors": sum(r.get("resolved", False) and r.get("loop", {}).get("tool_errors", 0) > 0 for r in rows),
        "tasks_with_repeated_commands": sum(r.get("loop", {}).get("repeated_commands", 0) > 0 for r in rows),
        "maximum_same_command_count": max((r.get("loop", {}).get("max_same_command", 0) for r in rows), default=0),
        "public_example_file_changes": [r["task_id"] for r in rows if r.get("example_tests_unchanged") is False],
        "main_model_stream_seconds": round(model_seconds, 3), "aggregate_agent_seconds": round(sum(all_times), 3),
        "other_agent_seconds_including_tools_memory_compaction": round(max(0, sum(all_times) - model_seconds), 3),
        "runtime_sources_unchanged": runtime_unchanged if report.get("runtime_source_hashes") else None,
        "memory_available_at_start": report.get("memory_available"), "memory_started_by_velora": report.get("memory_started_by_velora"),
        "outliers": [{key: r.get(key) for key in ("task_id", "resolved", "agent_status", "agent_reason", "error_type", "agent_seconds", "steps", "loop")}
                     for r in sorted(rows, key=lambda x: x.get("agent_seconds", x["seconds"]), reverse=True)[:12]],
        "official_leaderboard_comparable": False,
        "limits": ["公开旧题可能存在训练污染", "代码修复不能代表所有 Agent 工作", "模型与 loop 的影响尚未由对照实验分离", "同一任务的工具自检允许迭代，但隐藏测试不反馈给 Agent"],
    }


def tool_error_diagnostics(report):
    database = Path(report["root"]) / "runtime.db"
    if not database.is_file():
        return {"available": False}
    counts, examples = Counter(), {}
    edits, after_edits = {}, {}
    selected = {r.get("run_id"): r for r in report["results"] if r.get("run_id")}
    recovered = Counter()
    with sqlite3.connect("file:" + database.as_posix() + "?mode=ro", uri=True) as db:
        for run_id, snapshot in db.execute("select id,snapshot from agent_run_checkpoints"):
            if run_id not in selected:
                continue
            run = json.loads(snapshot)["run"]
            changed_at = []
            for index, tool in enumerate(run["tool_executions"]):
                execution_id = tool.get("execution_id")
                if not execution_id:
                    continue
                manifest = Path(report["root"]) / "checkpoints" / "records" / (hashlib.sha256(execution_id.encode()).hexdigest() + ".json")
                if not manifest.is_file():
                    continue
                value = json.loads(manifest.read_text(encoding="utf-8"))
                if "after" in value and value["before"]["files"].get("solution.py") != value["after"]["files"].get("solution.py"):
                    changed_at.append(index)
            edits[selected[run_id]["task_id"]] = len(changed_at)
            if changed_at:
                after_edits[selected[run_id]["task_id"]] = len(run["tool_executions"]) - changed_at[-1] - 1
            for tool in run["tool_executions"]:
                if not tool.get("is_error"):
                    continue
                try:
                    payload = json.loads(tool.get("result", "{}"))
                except ValueError:
                    continue
                error = payload.get("stderr", "") or payload.get("error", "")
                category = ("workspace_module_import" if "ModuleNotFoundError" in error else
                            "shell_syntax_or_command_resolution" if any(token in error for token in ("ParserError", "MissingFileSpecification", "is not recognized")) else
                            "empty_test_discovery" if "NO TESTS RAN" in error else
                            "assertion_or_test_failure" if any(token in error for token in ("AssertionError", "FAILED (", "Failed example:")) else
                            payload.get("reason") or "other_tool_error")
                counts[category] += 1
                recovered[category] += bool(selected[run_id].get("resolved"))
                examples.setdefault(category, {"task_id": selected[run_id]["task_id"], "tool": tool["name"], "error_tail": error[-800:]})
    return {"available": True, "error_calls_by_category": dict(counts), "calls_in_eventually_resolved_tasks": dict(recovered), "examples": examples,
            "solution_file_changes": edits, "tasks_with_multiple_solution_edits": sum(n > 1 for n in edits.values()),
            "tasks_without_solution_edits": [task for task, n in edits.items() if n == 0],
            "mean_tools_after_final_solution_edit": statistics.mean(after_edits.values()) if after_edits else None,
            "note": "测试断言失败可能是正常的修复前验证；命令语法和工作区导入错误体现工具使用摩擦，不直接等同 loop 死循环"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = summarize(report)
    result["tool_error_diagnostics"] = tool_error_diagnostics(report)
    output = args.report.with_name("analysis.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("run_status", "evaluated", "planned", "resolved", "failure_categories", "agent_seconds", "loop_totals")}, ensure_ascii=False))
