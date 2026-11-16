"""Isolated service restart with authenticated APIs and real native tool effects."""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from server.config import Settings
from server.core.provider.base import ChatResult, StreamEvent, ToolCall

pytestmark = pytest.mark.skipif(sys.platform != "win32" or os.environ.get("VELORA_TEST_WINDOWS_SANDBOX") != "1",
                              reason="requires opt-in Windows native execution")


@pytest.mark.asyncio
async def test_authenticated_task_acceptance_restart_output_and_undo(tmp_path, monkeypatch):
    from server import main
    from server.core.memory.client import AtomMemoryClient
    from server.core.provider.openai_compat import OpenAICompatProvider
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///" + (tmp_path / "runtime.db").as_posix(),
        start_memory_sidecar=False, reminders_enabled=False, mcp_enabled=False, web_search_enabled=False,
        xwiki_enabled=False, computer_enabled=True, agent_context_window=32000, llm_base_url="http://127.0.0.1:9/v1",
        llm_api_key="synthetic-test-key", control_token_path=str(tmp_path / "control.token"), skills_dir=str(tmp_path / "skills"),
        workspaces_dir=str(tmp_path / "workspaces"), execution_snapshots_dir=str(tmp_path / "checkpoints"),
        execution_policy_dir=str(tmp_path / "policies"), execution_output_dir=str(tmp_path / "output"),
        execution_receipts_dir=str(tmp_path / "receipts"), runtime_archive_dir=str(tmp_path / "archives"))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(AtomMemoryClient, "ensure_space", AsyncMock())
    monkeypatch.setattr(AtomMemoryClient, "recall", AsyncMock(return_value={}))
    monkeypatch.setattr(AtomMemoryClient, "add_source", AsyncMock(return_value=None))
    calls = []
    async def sample(self, messages, **kwargs):
        calls.append(messages)
        if not any(m.get("role") == "tool" for m in messages):
            result = ChatResult(tool_calls=[ToolCall("write", "write_file", '{"path":"result.json","content":"{\\"state\\":\\"ready\\"}"}')])
        else:
            result = ChatResult(content="已完成并验证文件。", finish_reason="stop")
        yield StreamEvent(final=result)
    monkeypatch.setattr(OpenAICompatProvider, "stream_chat", sample)
    app = main.create_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/sessions")).status_code == 401
            token = app.state.velora.control_token
            assert (await client.post("/api/auth/login", json={"token": token})).status_code == 200
            session = (await client.post("/api/sessions", json={})).json()["id"]
            response = await client.post("/api/chat/stream", json={"session_id": session, "text": "写入 JSON 结果文件。",
                "acceptance": [{"kind": "file", "path": "result.json", "contains": "ready", "json_valid": True}]})
            assert response.status_code == 200
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            terminal = next(frame for frame in frames if isinstance(frame.get("result"), dict) and frame["result"].get("run_id"))
            assert terminal["result"]["status"] == "completed", terminal
            assert terminal["result"]["verification"]["status"] == "passed"
            execution_id = next(frame["execution_id"] for frame in frames if frame.get("checkpoint_id"))
            run_id = terminal["result"]["run_id"]
            assert len(calls) == 2
            second = main.create_app()
            with pytest.raises(RuntimeError, match="Another service"):
                async with second.router.lifespan_context(second):
                    pass
    # Re-open the same persisted runtime, without reissuing any model/tool work.
    app = main.create_app()
    async with app.router.lifespan_context(app):
        assert app.state.velora.control_token == token
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://test",
                                     headers={"X-Velora-Token": token}) as client:
            assert (await client.get(f"/api/debug/runtime/checkpoints/{run_id}")).json()["outcome"]["status"] == "completed"
            restored = await client.post(f"/api/files/checkpoints/{execution_id}/restore", json={"session_id": session, "confirm": True})
            assert restored.status_code == 200, restored.text
            assert not (tmp_path / "workspaces" / session / "result.json").exists()
            submitted = await client.post("/api/execution", json={"session_id": session, "operation": "run_python",
                "arguments": {"code": "print('x'*20000 + 'END_SENTINEL')"}})
            assert submitted.status_code == 200, submitted.text
            identifier = submitted.json()["execution_id"]
            for _ in range(200):
                snapshot = (await client.get(f"/api/execution/{identifier}")).json()
                if snapshot["status"] not in ("prepared", "running"):
                    break
                await asyncio.sleep(.05)
            assert snapshot["status"] == "succeeded", snapshot
            output = await client.get(f"/api/execution/{identifier}/artifacts?offset=20000")
            assert output.json()["content"].strip() == "END_SENTINEL"
            assert len(calls) == 2
            submitted = await client.post("/api/execution", json={"session_id": session, "operation": "run_python",
                "arguments": {"code": "import pathlib,time; pathlib.Path('started').write_text('1'); time.sleep(30); pathlib.Path('after-cancel').write_text('bad')"}})
            identifier = submitted.json()["execution_id"]
            for _ in range(200):
                if (tmp_path / "workspaces" / session / "started").exists():
                    break
                await asyncio.sleep(.05)
            assert (tmp_path / "workspaces" / session / "started").exists()
            assert (await client.post(f"/api/execution/{identifier}/cancel")).json()["cancel_requested"]
            for _ in range(200):
                snapshot = (await client.get(f"/api/execution/{identifier}")).json()
                if snapshot["status"] not in ("prepared", "running"):
                    break
                await asyncio.sleep(.05)
            assert snapshot["status"] == "result_unknown"
            assert not (tmp_path / "workspaces" / session / "after-cancel").exists()
            assert (await client.post(f"/api/execution/{identifier}/reconcile")).json()["reconciled"] == 0
            assert "result_unknown" in (await client.get(f"/api/execution/{identifier}/events")).text
