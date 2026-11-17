from __future__ import annotations

import asyncio
import logging
from copy import copy, deepcopy
from itertools import count
from typing import Any

from server.core.chain.context import TurnContext
from server.core.agent.state import RunPhase, RunState, RunStatus, RunOutcome
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.tools.registry import ToolRegistry
from server.core.agent.tool_round import execute_tool_round
from server.core.agent.streaming import stream_round
from server.core.agent.recovery import is_transient, is_context_overflow
from server.core.agent.policy import StopAction, StopPolicy, RepeatedToolFailure, UncertainExecution
from server.core.agent.compaction import prepare_context, ContextBudgetError
from server.core.agent.storage_budget import StorageBudgetExceeded

logger = logging.getLogger(__name__)


class AgentLoop:
    """Own model-stage transitions; business post-processing stays in the chain."""

    def __init__(
        self,
        provider: OpenAICompatProvider | None = None,
        *,
        tools: ToolRegistry | None = None,
        max_steps: int | None = None,
        context_window: int | None = None,
        output_reserve: int = 4096,
        run_timeout: float | None = None,
        request_retries: int = 2,
        summary_output_tokens: int = 1024,
        stop_policies: tuple[StopPolicy, ...] = (),
        continuation_limit: int = 3,
        tool_failure_limit: int = 3,
    ) -> None:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive or None")
        if run_timeout is not None and run_timeout <= 0:
            raise ValueError("run_timeout must be positive or None")
        self._stop_policies = tuple(stop_policies)
        self._continuation_limit = max(1, continuation_limit)
        self._tool_failure_limit = max(1, tool_failure_limit)
        self._max_steps = max_steps
        self._context_window = context_window
        self._output_reserve = max(1, output_reserve)
        self._run_timeout = run_timeout
        self._request_retries = max(0, request_retries)
        self._summary_output_tokens = max(1, summary_output_tokens)
        self._provider = provider
        self._tools = tools

    def _provider_for(self, ctx: TurnContext) -> OpenAICompatProvider:
        if self._provider is not None:
            if isinstance(self._provider, OpenAICompatProvider):
                provider = copy(self._provider)
                provider.max_output_tokens = self._output_reserve
                return provider
            return self._provider
        return OpenAICompatProvider(ctx.llm_base_url, ctx.llm_api_key, max_output_tokens=self._output_reserve)

    async def run(self, ctx: TurnContext) -> RunOutcome:
        if self._run_timeout is None:
            return await self._run(ctx)
        try:
            async with asyncio.timeout(self._run_timeout) as deadline:
                return await self._run(ctx, deadline)
        except TimeoutError:
            state = ctx.agent_run
            outcome = state.outcome
            await ctx.publish("error", {"message": "Task time budget exhausted"})
            return outcome

    async def _run(self, ctx: TurnContext, deadline: asyncio.Timeout | None = None) -> RunOutcome:
        resumed = ctx.extras.pop("resume_checkpoint", None)
        if resumed is not None:
            if resumed["model"] != ctx.model:
                raise ValueError("Resume requires the original model")
            values = deepcopy(resumed["run"])
            values["phase"] = RunPhase(values["phase"])
            values["outcome"] = None
            state = RunState(**values)
            state.context_window = self._context_window or state.context_window
        else:
            state = RunState(messages=deepcopy(ctx.messages), context_window=self._context_window)
            from server.core.agent.acceptance import AcceptanceCheck
            criteria = ctx.request.client_meta.get("acceptance", [])
            if not isinstance(criteria, list) or len(criteria) > 20:
                raise ValueError("acceptance requires at most 20 checks")
            state.acceptance = [AcceptanceCheck.model_validate(item).model_dump() for item in criteria]
            if state.acceptance:
                import json
                state.messages.append({"role": "user", "content": "Acceptance criteria for this task: " + json.dumps(state.acceptance, ensure_ascii=False)})
        state.runtime_mode = "unified"
        ctx.agent_run = state
        if not ctx.llm_api_key and "localhost" not in ctx.llm_base_url and "127.0.0.1" not in ctx.llm_base_url:
            if not ctx.llm_base_url:
                ctx.stop_reason = "llm_not_configured"
                await ctx.publish("error", {"message": "LLM not configured"})
                return state.finish(RunStatus.FAILED, ctx.stop_reason)

        provider = self._provider_for(ctx)
        tools_payload: list[dict[str, Any]] = list(ctx.extras.get("tools") or [])
        use_tools = bool(tools_payload) and self._tools is not None
        messages = state.messages

        try:
            if resumed and resumed.get("resume_tool_batch"):
                from server.core.provider.base import ChatResult, ToolCall
                if self._tools is None:
                    raise UncertainExecution("Pending batch requires its tool registry")
                batch = [record for record in state.tool_executions if record.get("round") == state.step - 1]
                assistant = next(message for message in reversed(messages) if message.get("role") == "assistant" and message.get("tool_calls"))
                saved_result = ChatResult(content=assistant.get("content"), tool_calls=[
                    ToolCall(call["id"], call["function"]["name"], call["function"]["arguments"]) for call in assistant["tool_calls"]
                ])
                state.enter(RunPhase.EXECUTING_TOOLS)
                await execute_tool_round(ctx, self._tools, messages, saved_result, state.step - 1,
                                         failure_limit=self._tool_failure_limit, resume_records=batch)
            if state.context_window is None:
                discover = getattr(provider, "context_window", None)
                if discover is not None:
                    state.context_window = await discover(ctx.model)
            start_step = (state.step if resumed.get("resume_next_step") else max(0, state.step - 1)) if resumed else 0
            rounds = count(start_step)
            for round_i in rounds:
                if self._max_steps is not None and round_i >= self._max_steps:
                    break
                state.step = round_i + 1
                state.enter(RunPhase.REQUESTING)
                # One streaming request includes tools. A plain reply
                # (no tool_calls) finishes in that same request — no second generate.
                tools_payload = list(ctx.extras.get("tools") or [])
                round_tools = tools_payload if use_tools else None
                if state.context_window is None:
                    ctx.stop_reason = "context_capacity_unknown"
                    await ctx.publish("error", {"message": "Configure the model context window or use a service that reports capacity"})
                    return state.finish(RunStatus.FAILED, ctx.stop_reason)
                state.enter(RunPhase.COMPACTING)
                await prepare_context(
                    state, provider, ctx.model, round_tools, self._output_reserve,
                    summary_tokens=self._summary_output_tokens,
                )
                state.enter(RunPhase.REQUESTING)
                await ctx.save_checkpoint("request_ready")
                attempt = 0
                while True:
                    try:
                        result = await self._stream_round(ctx, provider, messages, tools=round_tools)
                        break
                    except Exception as exc:
                        if is_context_overflow(exc):
                            if state.context_recoveries >= 2:
                                raise ContextBudgetError("Provider context overflow persisted after bounded recovery") from exc
                            state.context_recoveries += 1
                            await ctx.publish("assistant_draft_reset", {"reason": "context_overflow", "round": round_i})
                            state.enter(RunPhase.COMPACTING)
                            await prepare_context(state, provider, ctx.model, round_tools, self._output_reserve,
                                                  summary_tokens=self._summary_output_tokens,
                                                  target_budget=max(1, int(state.estimated_input_tokens * .75)))
                            state.enter(RunPhase.REQUESTING)
                            await ctx.save_checkpoint("context_overflow_recovered")
                            continue
                        if attempt >= self._request_retries or not is_transient(exc):
                            raise
                        state.retries += 1
                        await ctx.publish("assistant_draft_reset", {"reason": "request_retry", "round": round_i})
                        await asyncio.sleep(min(2 ** attempt, 4))
                        attempt += 1
                if result.usage is not None:
                    state.latest_input_tokens = result.usage.input_tokens
                    state.input_tokens += result.usage.input_tokens
                    state.output_tokens += result.usage.output_tokens
                    state.cached_input_tokens += result.usage.cached_input_tokens
                if result.finish_reason == "length" and state.output_recoveries < 3:
                    state.output_recoveries += 1
                    if result.tool_calls:
                        messages.append({"role": "user", "content": "The previous tool call was truncated and was NOT executed. Return a complete valid tool call, using smaller operations if needed."})
                        state.answer_prefix = ""
                        await ctx.publish("assistant_draft_reset", {"reason": "incomplete_tool_call", "round": round_i})
                    elif result.content:
                        state.answer_prefix += result.content
                        messages.extend([{"role": "assistant", "content": result.content}, {"role": "user", "content": "Your response was cut off. Continue exactly where it ended without repeating earlier text. Finish the requested task."}])
                    else:
                        messages.append({"role": "user", "content": "The previous response was truncated before usable text. Return a complete concise response."})
                    await ctx.save_checkpoint("output_continuation_ready")
                    continue
                if result.finish_reason in {"length", "content_filter"}:
                    ctx.stop_reason = "output_truncated" if result.finish_reason == "length" else "output_filtered"
                    await ctx.publish("error", {"message": "Model output did not complete", "reason": ctx.stop_reason})
                    return state.finish(RunStatus.FAILED, ctx.stop_reason)
                state.enter(RunPhase.CHECKING_STOP)
                if not result.tool_calls:
                    if state.answer_prefix and not (result.content or "").strip():
                        ctx.stop_reason = "output_truncated"
                        return state.finish(RunStatus.FAILED, ctx.stop_reason)
                    answer = (state.answer_prefix + (result.content or "")).strip()
                    state.answer_prefix = ""
                    if answer:
                        if any(not e["task"].done() for e in ctx.extras.get("command_sessions", {}).values()):
                            messages.extend([{"role": "assistant", "content": answer}, {"role": "user", "content": "Commands are still running. Poll or cancel their command_session handles before finishing."}])
                            continue
                        decisions = []
                        for policy in self._stop_policies:
                            state.stop_checks += 1
                            async with asyncio.timeout(10):
                                decisions.append(await policy.on_stop(ctx, answer))
                        stop = next((d for d in decisions if d.action is StopAction.STOP), None)
                        wait = next((d for d in decisions if d.action is StopAction.WAIT), None)
                        if stop or wait:
                            if wait and not stop:
                                messages.append({"role": "assistant", "content": answer})
                            decision = stop or wait
                            ctx.stop_reason = decision.feedback or ("policy_stopped" if stop else "waiting_for_input")
                            return state.finish(RunStatus.FAILED if stop else RunStatus.WAITING, ctx.stop_reason)
                        feedback = [d.feedback.strip() for d in decisions if d.action is StopAction.CONTINUE]
                        if feedback:
                            if any(not text for text in feedback) or state.forced_continuations >= self._continuation_limit:
                                ctx.stop_reason = "continuation_limit"
                                return state.finish(RunStatus.FAILED, ctx.stop_reason)
                            state.forced_continuations += 1
                            messages.extend([{"role": "assistant", "content": answer}, {"role": "user", "content": "\n".join(feedback)}])
                            await ctx.publish("assistant_draft_reset", {"reason": "completion_check", "round": round_i})
                            continue
                    ctx.assistant_text = answer
                    ctx.messages = messages
                    if not ctx.assistant_text:
                        ctx.stop_reason = "empty_assistant"
                        await ctx.publish(
                            "error", {"message": "Empty model response"}
                        )
                        return state.finish(RunStatus.FAILED, ctx.stop_reason)
                    return state.finish(RunStatus.COMPLETED)

                if self._tools is None:
                    ctx.stop_reason = "llm_error"
                    await ctx.publish(
                        "error",
                        {"message": "Model requested tools but none registered"},
                    )
                    return state.finish(RunStatus.FAILED, ctx.stop_reason)

                state.enter(RunPhase.EXECUTING_TOOLS)
                state.answer_prefix = ""
                await execute_tool_round(
                    ctx, self._tools, messages, result, round_i,
                    failure_limit=self._tool_failure_limit,
                )

            ctx.stop_reason = "step_budget_exhausted"
            await ctx.publish(
                "error", {"message": "Tool loop exhausted without a final reply"}
            )
            return state.finish(RunStatus.FAILED, "step_budget_exhausted")
        except asyncio.CancelledError:
            if deadline is not None and deadline.expired():
                ctx.stop_reason = "runtime_timeout"
                state.finish(RunStatus.FAILED, ctx.stop_reason)
            else:
                state.finish(RunStatus.CANCELLED, "cancelled")
            raise
        except UncertainExecution as exc:
            ctx.stop_reason = "execution_result_unknown"
            await ctx.publish("error", {"message": str(exc)})
            return state.finish(RunStatus.FAILED, ctx.stop_reason)
        except RepeatedToolFailure as exc:
            ctx.stop_reason = "repeated_tool_failure"
            await ctx.publish("error", {"message": str(exc)})
            return state.finish(RunStatus.FAILED, ctx.stop_reason)
        except StorageBudgetExceeded as exc:
            ctx.stop_reason = "runtime_storage_budget_exhausted"
            await ctx.publish("error", {"message": str(exc), "reason": ctx.stop_reason})
            return state.finish(RunStatus.FAILED, ctx.stop_reason)
        except ContextBudgetError as exc:
            ctx.stop_reason = "context_budget_exhausted"
            await ctx.publish("error", {"message": str(exc)})
            return state.finish(RunStatus.FAILED, ctx.stop_reason)
        except Exception as exc:
            logger.exception("LLM tool loop failed")
            ctx.stop_reason = "llm_error"
            await ctx.publish("error", {"message": str(exc)})
            return state.finish(RunStatus.FAILED, ctx.stop_reason)

        finally:
            from server.core.execution.command_session import close_commands
            await close_commands(ctx)

    _stream_round = staticmethod(stream_round)
