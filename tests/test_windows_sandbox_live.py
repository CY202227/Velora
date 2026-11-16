"""Opt-in enforcement tests against the independently installed Windows runtime."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from server.config import Settings
from server.core.execution.service import execute

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("VELORA_TEST_WINDOWS_SANDBOX") != "1",
    reason="requires explicit native Windows enforcement test enablement",
)


def settings(tmp_path, timeout=10):
    return Settings(_env_file=None, computer_enabled=True, workspaces_dir=str(tmp_path / "workspaces"),
                    execution_policy_dir=str(tmp_path / "policies"), computer_timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_native_large_output_can_be_read_without_reexecution(tmp_path):
    from server.core.conversation.store import ConversationStore
    from server.core.execution.store import ExecutionStore
    from server.core.execution.artifacts import OutputStore
    s = settings(tmp_path)
    s.execution_output_dir = str(tmp_path / "output")
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "runtime.db").as_posix())
    await db.init()
    try:
        result = json.loads(await execute(s, "native", "run_python", {"code": "print('x'*20000 + 'END_SENTINEL')"},
                                         execution_id="large", store=ExecutionStore(db._session_factory)))
        assert result["ok"] and result["output_available"] and "_output" not in result
        page = OutputStore(s.execution_output_dir).read("large", "native", offset=20000)
        assert page["content"].strip() == "END_SENTINEL" and page["next_offset"] is None
    finally:
        await db.close()


async def run(s, operation, args):
    return json.loads(await execute(s, "native", operation, args))


@pytest.mark.asyncio
async def test_native_python_imports_workspace_with_future_annotations(tmp_path):
    s = settings(tmp_path)
    assert (await run(s, "write_file", {"path": "local_module.py", "content": "VALUE = 42\n"}))["ok"]
    result = await run(s, "run_python", {"code": "from __future__ import annotations\nfrom local_module import VALUE\nprint(VALUE)"})
    assert result["ok"] and result["stdout"].strip() == "42", result


@pytest.mark.asyncio
async def test_native_files_shell_and_credential_boundary(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setenv("VELORA_SYNTHETIC_SECRET", "must-not-inherit")
    assert (await run(s, "write_file", {"path": "hello.txt", "content": "你好"}))["ok"]
    assert (await run(s, "read_file", {"path": "hello.txt"}))["content"] == "你好"
    assert (await run(s, "list_dir", {"path": "."}))["ok"]
    result = await run(s, "run_shell", {"command": "Write-Output 'native-ready'"})
    assert result["ok"], result
    assert "native-ready" in result["stdout"]
    result = await run(s, "run_shell", {"command": "python -c \"print('python-path-ready')\""})
    assert result["ok"] and "python-path-ready" in result["stdout"], result
    result = await run(s, "run_python", {"code": "import os; print(os.environ.get('VELORA_SYNTHETIC_SECRET', 'absent'))"})
    assert result["ok"] and result["stdout"].strip() == "absent", result


@pytest.mark.asyncio
async def test_native_denies_outside_files_and_metadata(tmp_path):
    s = settings(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic baseline")
    root = tmp_path / "workspaces" / "native"
    (root / ".git").mkdir(parents=True)
    (root / ".git/config").write_text("synthetic metadata")
    for path in (outside, root / ".git/config"):
        for operation in ("read_text()", "write_text('changed')"):
            result = await run(s, "run_python", {"code": f"from pathlib import Path; Path({str(path)!r}).{operation}"})
            assert not result["ok"], result
    assert outside.read_text() == "synthetic baseline"
    assert (root / ".git/config").read_text() == "synthetic metadata"


@pytest.mark.asyncio
async def test_native_network_and_descendant_boundary(tmp_path):
    s = settings(tmp_path)
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        code = f"import socket; socket.create_connection(('127.0.0.1', {port}),timeout=2)"
        result = await run(s, "run_python", {"code": code})
        assert not result["ok"], result
        outside = tmp_path / "outside.txt"
        outside.write_text("synthetic baseline")
        child = f"from pathlib import Path; print(Path({str(outside)!r}).read_text())"
        result = await run(s, "run_python", {"code": f"import subprocess,sys; r=subprocess.run([sys.executable,'-I','-c',{child!r}]); sys.exit(r.returncode)"})
        assert not result["ok"], result
        assert "PermissionError" in result["stderr"], result
    finally:
        server.close()
        await server.wait_closed()


async def heartbeat_then_stop(tmp_path, cancel):
    s = settings(tmp_path, timeout=10 if cancel else 2)
    beat = tmp_path / "workspaces/native/heartbeat"
    loop = "from pathlib import Path; import time\nwhile True:\n Path('heartbeat').write_text(str(time.time_ns()))\n time.sleep(.05)"
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',{loop!r}]); time.sleep(60)"
    task = asyncio.create_task(run(s, "run_python", {"code": code}))
    try:
        for _ in range(100):
            if beat.exists():
                break
            if task.done():
                pytest.fail(f"workload exited before heartbeat: {task.result()}")
            await asyncio.sleep(.05)
        assert beat.exists()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert not result["ok"], result
        await asyncio.sleep(.3)
        before = beat.read_text()
        await asyncio.sleep(.3)
        assert beat.read_text() == before, "descendant survived execution stop"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_timeout_stops_descendants(tmp_path):
    await heartbeat_then_stop(tmp_path, cancel=False)


@pytest.mark.asyncio
async def test_native_cancellation_stops_descendants(tmp_path):
    await heartbeat_then_stop(tmp_path, cancel=True)


@pytest.mark.asyncio
async def test_native_output_is_bounded(tmp_path):
    result = await run(settings(tmp_path), "run_python", {"code": "import sys; print('x'*30000); print('e'*30000,file=sys.stderr)"})
    assert result["ok"], result
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert len(result["stdout"]) <= 12000 and len(result["stderr"]) <= 12000


@pytest.mark.asyncio
async def test_native_durable_execution_is_not_repeated(tmp_path):
    from server.core.conversation.store import ConversationStore
    from server.core.execution.store import ExecutionStore
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "executions.db").as_posix())
    await db.init()
    s = settings(tmp_path)
    try:
        store = ExecutionStore(db._session_factory)
        code = "from pathlib import Path; p=Path('counter'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('done')"
        first = await execute(s, "native", "run_python", {"code": code}, execution_id="durable-native", run_id="run", store=store)
        assert json.loads(first)["ok"], first
        restarted = ExecutionStore(db._session_factory)
        assert await execute(s, "native", "run_python", {"code": code}, execution_id="durable-native", run_id="run", store=restarted) == first
        assert (tmp_path / "workspaces/native/counter").read_text() == "x"
        assert (await restarted.read("durable-native"))["status"] == "succeeded"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_native_approval_gates_real_execution(tmp_path):
    from server.core.conversation.store import ConversationStore
    from server.core.execution.store import ExecutionStore
    from server.core.execution.approvals import ApprovalCoordinator, ApprovalStore
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "approval.db").as_posix())
    await db.init()
    s = settings(tmp_path)
    s.execution_require_approval = True
    coordinator = ApprovalCoordinator(ApprovalStore(db._session_factory))
    notified = asyncio.Queue()
    marker = tmp_path / "workspaces/native/approved"
    async def review(identifier, digest, target):
        async def publish(approval_id):
            await notified.put(approval_id)
        return await coordinator.wait(execution_id=identifier, session_id="native", digest=digest,
                                      target=target, publish=publish)
    task = asyncio.create_task(execute(s, "native", "run_python", {"code": "from pathlib import Path; Path('approved').write_text('once')"},
                                       execution_id="native-approved", store=ExecutionStore(db._session_factory),
                                       request_approval=review))
    try:
        identifier = await asyncio.wait_for(notified.get(), 3)
        assert not marker.exists()
        await coordinator.decide(identifier, session_id="native", approve=True)
        result = json.loads(await task)
        assert result["ok"], result
        assert marker.read_text() == "once"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await db.close()


@pytest.mark.asyncio
async def test_native_file_checkpoint_can_restore_without_git(tmp_path):
    from server.core.computer.workspace import workspace_root
    from server.core.conversation.store import ConversationStore
    from server.core.execution.store import ExecutionStore
    from server.core.execution.snapshots import SnapshotService
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "snapshots.db").as_posix())
    await db.init()
    s = settings(tmp_path)
    root = workspace_root(s.workspaces_dir, "native")
    (root / "file.txt").write_text("human baseline")
    snapshots = SnapshotService(tmp_path / "private-snapshots")
    try:
        result = json.loads(await execute(s, "native", "run_python", {
            "code": "from pathlib import Path; Path('file.txt').write_text('agent change'); Path('new.txt').write_text('new')",
        }, execution_id="native-file-checkpoint", run_id="run", store=ExecutionStore(db._session_factory), snapshots=snapshots))
        assert result["ok"], result
        assert result["checkpoint_id"] == "native-file-checkpoint"
        assert not (root / ".git").exists()
        async with snapshots.lock(root):
            restored = await asyncio.to_thread(snapshots.restore, result["checkpoint_id"], "native", root)
        assert (root / "file.txt").read_text() == "human baseline"
        assert not (root / "new.txt").exists()
        assert len(restored["restored_paths"]) == 2
    finally:
        await db.close()


async def approved_scope(s, db, operation, arguments):
    from server.core.execution.approvals import ApprovalCoordinator, ApprovalStore
    from server.core.execution.store import ExecutionStore
    coordinator = ApprovalCoordinator(ApprovalStore(db._session_factory))
    async def confirm(identifier, digest, target, permission_scope):
        assert permission_scope["host_access"] is False
        async def publish(approval_id):
            await coordinator.decide(approval_id, session_id="native", approve=True)
        return await coordinator.wait(execution_id=identifier, session_id="native", digest=digest,
                                      target=target, publish=publish)
    return json.loads(await execute(s, "native", operation, arguments,
                                    store=ExecutionStore(db._session_factory), request_approval=confirm))


@pytest.mark.asyncio
async def test_native_approved_read_path_stays_readonly(tmp_path):
    from server.core.conversation.store import ConversationStore
    outside = tmp_path / "documents"
    outside.mkdir()
    file = outside / "reference.txt"
    file.write_text("synthetic reference")
    s = settings(tmp_path)
    s.execution_read_allowlist = [str(outside)]
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "read-scope.db").as_posix())
    await db.init()
    try:
        denied = await run(s, "read_file", {"path": str(file)})
        assert not denied["ok"], denied
        permissions = {"read_paths": [str(outside)], "reason": "read a synthetic reference"}
        result = await approved_scope(s, db, "read_file", {"path": str(file), "permissions": permissions})
        assert result["ok"] and result["content"] == "synthetic reference", result
        result = await approved_scope(s, db, "run_python", {
            "code": f"from pathlib import Path; Path({str(file)!r}).write_text('changed')", "permissions": permissions})
        assert not result["ok"], result
        assert file.read_text() == "synthetic reference"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_public_network_grant_keeps_loopback_blocked(tmp_path):
    from server.core.conversation.store import ConversationStore
    s = settings(tmp_path)
    s.execution_allow_public_network = True
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "network-scope.db").as_posix())
    await db.init()
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        code = f"import socket\ntry:\n socket.create_connection(('127.0.0.1',{port}),timeout=2)\n print('reachable')\nexcept OSError:\n print('blocked')"
        result = await approved_scope(s, db, "run_python", {"code": code, "permissions": {
            "public_network": True, "reason": "test public egress without local control access"}})
        assert result["ok"] and result["stdout"].strip() == "blocked", result
    finally:
        server.close()
        await server.wait_closed()
        await db.close()


@pytest.mark.asyncio
async def test_explicit_host_grant_uses_account_access_with_workspace_only_undo(tmp_path):
    from server.core.conversation.store import ConversationStore
    from server.core.execution.approvals import ApprovalCoordinator, ApprovalStore
    from server.core.execution.store import ExecutionStore
    from server.core.execution.snapshots import SnapshotService
    s = settings(tmp_path)
    s.execution_allow_host = True
    outside = tmp_path / "synthetic-outside.txt"
    outside.write_text("baseline")
    snapshots = SnapshotService(tmp_path / "private-snapshots")
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "host-grant.db").as_posix())
    await db.init()
    coordinator = ApprovalCoordinator(ApprovalStore(db._session_factory))
    async def confirm(identifier, digest, target, permission_scope):
        assert permission_scope["host_access"] is True
        async def publish(approval_id):
            await coordinator.decide(approval_id, session_id="native", approve=True)
        return await coordinator.wait(execution_id=identifier, session_id="native", digest=digest, target=target, publish=publish)
    try:
        store = ExecutionStore(db._session_factory)
        result = json.loads(await execute(s, "native", "run_python", {
            "code": f"from pathlib import Path; Path({str(outside)!r}).write_text('host change'); Path('inside.txt').write_text('workspace change')",
            "permissions": {"host_access": True, "reason": "synthetic host permission test"},
        }, execution_id="host-grant", store=store, request_approval=confirm, snapshots=snapshots))
        assert result["ok"] and result["backend"] == "host-approved", result
        assert (await store.read("host-grant"))["backend"] == "host-approved"
        assert result["checkpoint_coverage"] == "workspace_only_host_execution"
        assert outside.read_text() == "host change"
        root = tmp_path / "workspaces/native"
        snapshots.restore("host-grant", "native", root)
        assert not (root / "inside.txt").exists()
        assert outside.read_text() == "host change"
    finally:
        await db.close()
