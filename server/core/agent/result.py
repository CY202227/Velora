"""Public terminal result, distinct from a successful assistant answer."""


def terminal_result(ctx) -> dict:
    run = ctx.agent_run
    reason = ctx.stop_reason or (run.outcome.reason if run and run.outcome else None)
    status = run.outcome.status.value if run and run.outcome else ("failed" if reason else "completed")
    if reason == "cancelled":
        status = "cancelled"
    elif reason and status == "completed":
        status = "failed"
    messages = {
        "step_budget_exhausted": "任务尚未完成：已达到本次执行步数预算。",
        "runtime_timeout": "任务尚未完成：已达到本次执行时间预算。",
        "context_budget_exhausted": "任务尚未完成：上下文无法在当前容量内安全继续。",
        "runtime_storage_budget_exhausted": "任务未完成：运行存储预算不足，已停止，请先处理历史记录或调整预算。",
        "context_capacity_unknown": "任务尚未开始：无法确认模型上下文容量。",
        "output_truncated": "任务尚未完成：模型输出被截断。",
        "output_filtered": "任务尚未完成：模型未返回完整可用结果。",
        "repeated_tool_failure": "任务尚未完成：相同工具调用持续失败，已停止重试。",
        "continuation_limit": "任务尚未完成：结束检查反复要求继续，已停止自动推进。",
        "checkpoint_failed": "运行记录保存失败，无法确认安全恢复位置，请勿盲目重试外部操作。",
        "execution_result_unknown": "任务已停止：工具执行结果无法确认，请先核对执行记录，不要重复执行可能产生副作用的操作。",
        "cancelled": "任务已取消；已发生的外部操作不会自动撤销。",
    }
    message = "" if status == "completed" else messages.get(reason, "任务正在等待补充输入。" if status == "waiting" else "任务未完成，请查看停止原因及工具执行状态。")
    tools = run.tool_executions if run else []
    return {
        "status": status, "reason": reason, "message": message,
        "run_id": run.run_id if run else None, "step": run.step if run else 0,
        "verification": (run.verification if run else None) or {"status": "not_requested"},
        "tools_returned": sum(t["status"] == "returned" for t in tools),
        "tools_result_unknown": sum(t["status"] in {"running", "result_unknown"} for t in tools),
        "resume_supported": status == "waiting" and not any(t["status"] != "returned" for t in tools),
    }
