import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from server.core.agent.compaction import ContextBudgetError, prepare_context
from server.core.agent.state import RunState
from server.core.chain.router import TurnRouter
from server.core.chain.locks import SessionLockManager
from server.core.chain.wait_registry import WaitRegistry
from server.core.chain.types import TurnRequest
from server.core.provider.openai_compat import OpenAICompatProvider


@pytest.mark.asyncio
async def test_cancel_bypasses_lock_and_next_turn_runs():
    entered = asyncio.Event()
    calls = 0
    async def execute(ctx, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return ctx
    router = TurnRouter(SimpleNamespace(execute=execute), SessionLockManager(), WaitRegistry())
    events = []
    async def emit(event):
        events.append(event)
    first = asyncio.create_task(router.handle(TurnRequest("s", "first"), emit=emit))
    await entered.wait()
    second = asyncio.create_task(router.handle(TurnRequest("s", "second")))
    assert router.cancel("s")
    assert not router.cancel("s")
    result = await asyncio.wait_for(first, 1)
    assert result.stop_reason == "cancelled"
    assert [e.type for e in events] == ["turn_start", "turn_end"]
    assert (await asyncio.wait_for(second, 1)).stop_reason is None
    assert not router.cancel("s")


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_cleans_registry():
    entered = asyncio.Event()
    async def execute(ctx, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    router = TurnRouter(SimpleNamespace(execute=execute), SessionLockManager(), WaitRegistry())
    task = asyncio.create_task(router.handle(TurnRequest("s", "test")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not router.cancel("s")


@pytest.mark.asyncio
async def test_provider_count_allows_multimodal_admission():
    state = RunState(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/image"}}]}], context_window=10000)
    provider = SimpleNamespace(count_input_tokens=AsyncMock(return_value=1500))
    await prepare_context(state, provider, "model", [{"type": "function"}], 1000)
    assert state.estimated_input_tokens == 1500
    assert state.token_count_source == "provider"
    assert provider.count_input_tokens.await_args.kwargs["tools"]


@pytest.mark.asyncio
async def test_counter_unavailable_falls_back_only_for_text():
    provider = SimpleNamespace(count_input_tokens=AsyncMock(return_value=None))
    state = RunState(messages=[{"role": "user", "content": "hello"}], context_window=10000)
    await prepare_context(state, provider, "model", None, 1000)
    assert state.token_count_source == "estimate"
    state.messages[0]["content"] = [{"type": "image_url"}]
    with pytest.raises(ContextBudgetError, match="Multimodal"):
        await prepare_context(state, provider, "model", None, 1000)


@pytest.mark.asyncio
async def test_count_endpoint_preserves_chat_request_and_tool_schema(monkeypatch):
    seen = []
    def handle(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"count": 321})
    real_client = httpx.AsyncClient
    monkeypatch.setattr("server.core.provider.openai_compat.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    provider = OpenAICompatProvider("https://provider.test/v1", "key", token_count_path="/tokenize")
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    assert await provider.count_input_tokens([{"role": "user", "content": "hello"}], model="m", tools=tools) == 321
    assert seen[0]["tools"] == tools
    assert seen[0]["model"] == "m"


def test_counter_cannot_send_credentials_to_another_origin():
    with pytest.raises(ValueError):
        OpenAICompatProvider("https://provider.test/v1", "key", token_count_path="//other.test/tokenize")


@pytest.mark.asyncio
async def test_model_capacity_falls_back_to_exact_entry_in_model_list(monkeypatch):
    def handle(request):
        if request.url.path.endswith("/models/target"):
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "other", "max_model_len": 999}, {"id": "target", "max_model_len": 131072}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr("server.core.provider.openai_compat.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    provider = OpenAICompatProvider("https://provider.test/v1", "key")
    assert await provider.context_window("target") == 131072


@pytest.mark.asyncio
async def test_all_sessions_use_unified_runtime_without_mutating_shared_provider():
    from server.core.nodes.llm import LLMNode
    from server.core.chain.context import TurnContext
    from server.core.chain.types import TurnRequest, NodeResult
    from server.core.provider.base import ChatResult
    provider = OpenAICompatProvider("http://localhost/v1", "")
    node = LLMNode(provider, context_window=32000)
    node._stream_round = AsyncMock(return_value=ChatResult(content="ok"))
    for session_id, expected in [("canary", "unified"), ("normal", "unified"), ("canary", "unified")]:
        ctx = TurnContext(request=TurnRequest(session_id, "hello"), session_id=session_id, llm_base_url="http://localhost")
        assert await node.process(ctx) is NodeResult.CONTINUE
        assert ctx.agent_run.runtime_mode == expected
    assert provider.max_output_tokens is None


@pytest.mark.asyncio
async def test_cancel_api_checks_session_and_reports_idle():
    from fastapi import FastAPI
    from server.api.chat import router
    app = FastAPI()
    app.include_router(router)
    cancel = Mock(return_value=False)
    store = SimpleNamespace(get_session=AsyncMock(side_effect=[None, object()]))
    app.state.velora = SimpleNamespace(store=store, router=SimpleNamespace(cancel=cancel))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/chat/cancel", json={"session_id": "missing"})).status_code == 404
        response = await client.post("/api/chat/cancel", json={"session_id": "idle"})
        assert response.json() == {"cancelled": False}
    cancel.assert_called_once_with("idle")


@pytest.mark.asyncio
async def test_stream_retains_finish_reason_from_last_choice(monkeypatch):
    import json
    data = [{"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": "length"}]}]
    payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in data) + "data: [DONE]\n\n"
    real_client = httpx.AsyncClient
    monkeypatch.setattr("server.core.provider.openai_compat.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=payload)), **kw))
    provider = OpenAICompatProvider("https://provider.test/v1", "key")
    events = [e async for e in provider.stream_chat([], model="m")]
    assert events[-1].final.finish_reason == "length"
    assert events[-1].final.content == "partial"


@pytest.mark.asyncio
async def test_budget_exhaustion_has_explicit_non_success_terminal_result():
    from server.core.chain.context import TurnContext
    from server.core.agent.state import RunState, RunStatus
    async def execute(ctx, **kwargs):
        ctx.agent_run = RunState(step=10)
        ctx.stop_reason = "step_budget_exhausted"
        ctx.agent_run.finish(RunStatus.FAILED, ctx.stop_reason)
        return ctx
    router = TurnRouter(SimpleNamespace(execute=execute), SessionLockManager(), WaitRegistry())
    events = []
    async def emit(event):
        events.append(event)
    await router.handle(TurnRequest("s", "task"), emit=emit)
    result = events[-1].data["result"]
    assert events[-1].type == "turn_end"
    assert result["status"] == "failed"
    assert result["reason"] == "step_budget_exhausted"
    assert result["message"]
    assert result["step"] == 10
    assert not result["resume_supported"]


@pytest.mark.asyncio
async def test_checkpoint_failure_still_returns_failure_and_releases_registration():
    from server.core.agent.state import RunState, RunStatus
    async def execute(ctx, **kwargs):
        ctx.agent_run = RunState()
        ctx.agent_run.finish(RunStatus.COMPLETED)
        return ctx
    store = SimpleNamespace(checkpoint=AsyncMock(side_effect=OSError("offline")))
    router = TurnRouter(SimpleNamespace(execute=execute), SessionLockManager(), WaitRegistry(), run_store=store)
    events = []
    async def emit(event):
        events.append(event)
    await router.handle(TurnRequest("s", "task"), emit=emit)
    assert events[-1].data["result"]["status"] == "failed"
    assert events[-1].data["result"]["reason"] == "checkpoint_failed"
    assert not router.runtime_status()["active"]
