from scripts.analyze_benchmark import summarize


def test_incomplete_benchmark_distinguishes_completed_wrong_answer_and_deadline():
    report = {"benchmark": "fixture", "model": "fixture", "status": "running", "selected_ids": ["a", "b", "c", "d"],
        "results": [
            {"task_id": "a", "resolved": True, "baseline_tests_failed": True, "agent_seconds": 5, "seconds": 6, "steps": 2,
             "loop": {"tool_errors": 1, "repeated_commands": 0}, "model_rounds": [{"seconds": 3}]},
            {"task_id": "b", "resolved": False, "agent_status": "completed", "baseline_tests_failed": True,
             "agent_seconds": 7, "seconds": 8, "steps": 3, "grading": {"passed": False}},
            {"task_id": "c", "resolved": False, "agent_status": "cancelled", "error_type": "TimeoutError",
             "agent_seconds": 180, "seconds": 181, "steps": 20, "loop": {"unknown_results": 1}},
        ]}
    summary = summarize(report)
    assert summary["run_status"] == "running" and summary["evaluated"] == 3 and summary["planned"] == 4
    assert summary["resolved_rate_of_evaluated"] == 1 / 3
    assert summary["failure_categories"] == {"resolved": 1, "completed_but_hidden_tests_failed": 1, "evaluation_deadline": 1}
    assert summary["resolved_despite_tool_errors"] == 1
    assert summary["loop_totals"]["unknown_results"] == 1
    assert summary["agent_seconds"]["p95"] == 180
    assert summary["reference_controls"]["resolved_initially_failing"] == 1
