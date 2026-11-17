from __future__ import annotations
import asyncio
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5
from server.core.agent.policy import RepeatedToolFailure, UncertainExecution
from typing import Any
from server.core.chain.context import TurnContext
from server.core.provider.base import ChatResult
from server.core.tools.registry import ToolRegistry
from server.core.tools.runner import execute_tool

async def execute_tool_round(ctx: TurnContext, registry: ToolRegistry, messages: list[dict[str, Any]], result: ChatResult, round_i: int, *, failure_limit: int | None = None, resume_records=None) -> None:
    # Providers can stream a natural-language preamble before the
    # final chunk reveals tool_calls. That text is a draft, not the
    # answer: tell clients to clear it before we execute actions,
    # otherwise the eventual answer gets glued to stale narration.
    await ctx.publish(
        "assistant_draft_reset",
        {"reason": "tool_call", "round": round_i},
    )
    if resume_records is None:
        messages.append(
            {
                "role": "assistant",
                "content": result.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in result.tool_calls
                ],
            }
        )

    records = resume_records if resume_records is not None else [{"id": tc.id, "name": tc.name, "round": round_i, "status": "not_started"} for tc in result.tool_calls]
    if ctx.agent_run is not None and resume_records is None:
        ctx.agent_run.tool_executions.extend(records)
    for position, (tc, record) in enumerate(zip(result.tool_calls, records)):
        if resume_records is not None and record["status"] == "returned":
            continue
        if record["status"] != "not_started":
            raise UncertainExecution("Only unstarted tools can be resumed")
        await ctx.publish(
            "tool_call",
            {
                "id": tc.id,
                "name": tc.name,
                "arguments": tc.arguments,
                "round": round_i,
            },
        )
        record["status"] = "running"
        if tc.name in ("run_shell", "run_python"):
            try:
                source = json.loads(tc.arguments).get("command" if tc.name == "run_shell" else "code")
                if isinstance(source, str):
                    record["source_digest"] = hashlib.sha256(source.encode()).hexdigest()
            except (ValueError, AttributeError):
                pass
        if ctx.agent_run is not None and tc.name in ("run_shell", "run_python", "read_file", "write_file", "list_dir", "command_session"):
            record["execution_id"] = uuid5(NAMESPACE_URL, f"velora:{ctx.agent_run.run_id}:{round_i}:{position}:{tc.id}").hex
        await ctx.save_checkpoint("tool_started")
        previous_execution_id = ctx.extras.get("execution_id")
        if "execution_id" in record:
            ctx.extras["execution_id"] = record["execution_id"]
        try:
            out = await execute_tool(registry, tc.name, tc.arguments, ctx)
        except asyncio.CancelledError:
            record["status"] = "result_unknown"
            raise
        except Exception:
            record["status"] = "failed"
            raise
        finally:
            if previous_execution_id is None:
                ctx.extras.pop("execution_id", None)
            else:
                ctx.extras["execution_id"] = previous_execution_id
        record["status"] = "returned"
        record["result"] = out
        try:
            payload = json.loads(out)
        except (ValueError, TypeError):
            payload = None
        failed = isinstance(payload, dict) and (payload.get("ok") is False or bool(payload.get("error")))
        if isinstance(payload, dict) and type(payload.get("workspace_changed")) is bool:
            record["workspace_changed"] = payload["workspace_changed"]
        uncertain = isinstance(payload, dict) and (
            payload.get("status") == "result_unknown" or payload.get("reason") == "execution_not_replayed"
        )
        if uncertain:
            record["status"] = "result_unknown"
        record["is_error"] = failed
        await ctx.save_checkpoint("tool_result_unknown" if uncertain else "tool_returned")
        await ctx.publish(
            "tool_result",
            {
                "id": tc.id,
                "name": tc.name,
                "result": out[:2000],
                "execution_id": record.get("execution_id"),
                "checkpoint_id": payload.get("checkpoint_id") if isinstance(payload, dict) else None,
                "checkpoint_coverage": payload.get("checkpoint_coverage") if isinstance(payload, dict) else None,
                "round": round_i,
            },
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": out,
            }
        )

        if uncertain:
            raise UncertainExecution("Execution result is unknown; automatic continuation stopped to avoid duplicate effects")

        if failure_limit is not None and ctx.agent_run is not None:
            state = ctx.agent_run
            if failed:
                try:
                    arguments = json.dumps(json.loads(tc.arguments), sort_keys=True)
                except (ValueError, TypeError):
                    arguments = tc.arguments
                normalized_output = json.dumps({k: v for k, v in payload.items() if k not in {"execution_id", "checkpoint_id"}}, sort_keys=True) if isinstance(payload, dict) else out
                fingerprint = hashlib.sha256((tc.name + arguments + normalized_output).encode()).hexdigest()
                state.consecutive_tool_failures = state.consecutive_tool_failures + 1 if state.failure_fingerprint == fingerprint else 1
                state.failure_fingerprint = fingerprint
                if state.consecutive_tool_failures >= failure_limit:
                    raise RepeatedToolFailure("Identical tool invocation repeatedly returned the same error")
            else:
                state.failure_fingerprint = None
                state.consecutive_tool_failures = 0
