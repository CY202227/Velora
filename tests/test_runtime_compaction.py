import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from server.core.agent.compaction import ContextBudgetError, compactable_range, prepare_context
from server.core.agent.state import RunState
from server.core.provider.base import ChatResult, TokenUsage


def history():
    return [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "current task"},
        {"role": "assistant", "content": "a" * 5000, "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "old result"},
        {"role": "assistant", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "latest evidence"},
    ]


@pytest.mark.asyncio
async def test_summary_commits_atomically_and_preserves_original_history():
    original = history()
    state = RunState(messages=deepcopy(original), context_window=8000)
    provider = type("Provider", (), {"chat": AsyncMock(return_value=ChatResult(content="Old lookup completed; next action pending.", usage=TokenUsage(100, 20)))})()
    await prepare_context(state, provider, "m", None, 4000)
    assert state.messages[:2] == original[:2]
    assert state.messages[-2:] == original[-2:]
    assert "compacted-summary" in state.messages[2]["content"]
    assert state.messages[2]["role"] == "assistant"
    assert state.context_archive == [original]
    assert state.compactions == 1
    assert state.input_tokens == 100
    assert state.latest_input_tokens is None
    assert provider.chat.await_args.kwargs["max_output_tokens"] == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [ChatResult(), ChatResult(content="x" * 10000), ChatResult(content="<think>private</think>")])
async def test_invalid_summary_does_not_mutate_history(reply):
    state = RunState(messages=history(), context_window=8000)
    snapshot = deepcopy(state.messages)
    provider = type("Provider", (), {"chat": AsyncMock(return_value=reply)})()
    with pytest.raises(ContextBudgetError):
        await prepare_context(state, provider, "m", None, 4000)
    assert state.messages == snapshot
    assert not state.context_archive


@pytest.mark.asyncio
async def test_summary_failure_preserves_even_uncommitted_pruning():
    state = RunState(messages=history(), context_window=12000)
    state.messages[3]["content"] = "evidence" * 1000
    original = deepcopy(state.messages)
    provider = type("Provider", (), {"chat": AsyncMock(side_effect=RuntimeError("unavailable"))})()
    with pytest.raises(RuntimeError, match="unavailable"):
        await prepare_context(state, provider, "m", None, 8000)
    assert state.messages == original
    assert state.pruned_results == 0
    assert state.compaction_attempts == 1


@pytest.mark.asyncio
async def test_cancellation_during_summary_leaves_history_untouched():
    state = RunState(messages=history(), context_window=8000)
    original = deepcopy(state.messages)
    entered = asyncio.Event()
    async def summarize(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    provider = type("Provider", (), {})()
    provider.chat = summarize
    task = asyncio.create_task(prepare_context(state, provider, "m", None, 4000))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.messages == original


def test_open_tool_batch_is_not_selected():
    assert compactable_range(history()) == (2, 4)
    messages = history()
    messages[3]["tool_call_id"] = "unrelated"
    assert compactable_range(messages) is None


@pytest.mark.asyncio
async def test_source_that_cannot_fit_does_not_start_summary():
    state = RunState(messages=history(), context_window=1000)
    provider = type("Provider", (), {"chat": AsyncMock()})()
    with pytest.raises(ContextBudgetError):
        await prepare_context(state, provider, "m", None, 500)
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_state_change_discards_summary():
    state = RunState(messages=history(), context_window=8000)
    async def summarize(*args, **kwargs):
        state.messages.append({"role": "user", "content": "new requirement"})
        return ChatResult(content="brief")
    provider = type("Provider", (), {})()
    provider.chat = summarize
    with pytest.raises(ContextBudgetError, match="Run changed"):
        await prepare_context(state, provider, "m", None, 4000)
    assert state.messages[-1]["content"] == "new requirement"
    assert not state.context_archive
