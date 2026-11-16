import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.api.files import router
from server.config import Settings
from server.core.chain.locks import SessionLockManager
from server.core.conversation.store import ConversationStore
from server.core.execution.service import execute
from server.core.execution.snapshots import SnapshotError, SnapshotService
from server.core.execution.store import ExecutionStore


def workspace(tmp_path):
    root = tmp_path / "workspaces/s"
    root.mkdir(parents=True)
    return root, SnapshotService(tmp_path / "private-checkpoints")


def test_retention_preserves_unfinished_and_other_runs(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("base")
    old = snapshots.begin(root, "old", "s", "expired")
    (root / "file").write_text("changed")
    snapshots.finish(old)
    snapshots.begin(root, "pending", "s", "expired")
    recent = snapshots.begin(root, "other", "s", "retained")
    snapshots.finish(recent)
    import time
    for path in (snapshots.directory / "records").glob("*.json"):
        os.utime(path, (time.time() - 40 * 86400,) * 2)
    assert snapshots.prune({"expired"}, 30)["records_removed"] == 1
    assert snapshots.load("pending", "s", root)["state"] == "prepared"
    assert snapshots.load("other", "s", root)["state"] == "ready"
    assert snapshots.preview("other", "s", root)["conflicts"] == []


def test_finish_rejects_replaced_workspace_root(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("base")
    value = snapshots.begin(root, "e", "s", "r")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("must not capture")
    import subprocess
    import sys
    original = root.with_name("original")
    assert original.resolve().is_relative_to(tmp_path.resolve())
    assert root.resolve().is_relative_to(tmp_path.resolve())
    root.rename(original)
    if sys.platform == "win32":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(root), str(outside)], check=True, capture_output=True)
    else:
        root.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(SnapshotError, match="link"):
            snapshots.finish(value)
        assert snapshots._manifest("e").read_text().find("private") == -1
    finally:
        if sys.platform == "win32":
            os.rmdir(root)
        else:
            root.unlink()


def test_restore_modified_added_deleted_and_keep_user_git(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "modified").write_text("before")
    (root / "deleted").write_bytes(b"original\0binary")
    (root / ".git").mkdir()
    (root / ".git/index").write_text("user staging")
    value = snapshots.begin(root, "e", "s", "r")
    (root / "modified").write_text("after")
    (root / "deleted").unlink()
    (root / "added").write_text("agent")
    snapshots.finish(value)
    preview = snapshots.preview("e", "s", root)
    assert {change["kind"] for change in preview["changes"]} == {"modified", "added", "deleted"}
    assert not preview["conflicts"]
    result = snapshots.restore("e", "s", root)
    assert result["coverage"] == "partial"
    assert (root / "modified").read_text() == "before"
    assert (root / "deleted").read_bytes() == b"original\0binary"
    assert not (root / "added").exists()
    assert (root / ".git/index").read_text() == "user staging"
    assert snapshots.restore("e", "s", root)["already_restored"]


def test_later_human_changes_block_entire_restore(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "one").write_text("base")
    (root / "two").write_text("base")
    value = snapshots.begin(root, "e", "s", "r")
    (root / "one").write_text("agent")
    (root / "two").write_text("agent")
    snapshots.finish(value)
    (root / "two").write_text("human edit")
    with pytest.raises(SnapshotError, match="changed after"):
        snapshots.restore("e", "s", root)
    assert (root / "one").read_text() == "agent"
    assert (root / "two").read_text() == "human edit"


def test_private_content_and_session_boundaries(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / ".env").write_text("synthetic-secret")
    (root / "file").write_text("base")
    value = snapshots.begin(root, "e", "s", "r")
    snapshots.finish(value)
    blobs = list((snapshots.directory / "blobs").iterdir())
    assert all(b"synthetic-secret" not in path.read_bytes() for path in blobs)
    with pytest.raises(SnapshotError):
        snapshots.preview("e", "foreign", root)
    with pytest.raises(SnapshotError):
        snapshots._read(root, "../outside")


def test_capture_budgets_and_hardlinks_fail_closed(tmp_path):
    root, snapshots = workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("synthetic data")
    try:
        os.link(outside, root / "linked")
    except OSError:
        pytest.skip("hardlinks unavailable")
    with pytest.raises(SnapshotError, match="single-link"):
        snapshots.begin(root, "e", "s", "r")


def test_incomplete_or_tampered_checkpoint_cannot_restore(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("base")
    value = snapshots.begin(root, "e", "s", "r")
    with pytest.raises(SnapshotError):
        snapshots.preview("e", "s", root)
    (root / "file").write_text("agent")
    snapshots.finish(value)
    digest = value["before"]["files"]["file"]["hash"]
    (snapshots.directory / "blobs" / digest).write_bytes(b"corrupted")
    with pytest.raises(SnapshotError, match="integrity"):
        snapshots.restore("e", "s", root)
    assert (root / "file").read_text() == "agent"


def test_total_storage_budget_and_baseline_blob_integrity(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("base")
    snapshots.max_storage_bytes = 1
    with pytest.raises(SnapshotError, match="storage budget"):
        snapshots.begin(root, "too-large", "s", "r")
    snapshots.max_storage_bytes = 1024 * 1024
    value = snapshots.begin(root, "good", "s", "r")
    digest = value["before"]["files"]["file"]["hash"]
    (snapshots.directory / "blobs" / digest).write_bytes(b"tampered")
    with pytest.raises(SnapshotError, match="integrity"):
        snapshots.begin(root, "next", "s", "r")


def test_whole_run_restore_combines_operations_and_consumes_members(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("human baseline")
    first = snapshots.begin(root, "one", "s", "r")
    (root / "file").write_text("agent first")
    snapshots.finish(first)
    second = snapshots.begin(root, "two", "s", "r")
    (root / "file").write_text("agent second")
    snapshots.finish(second)
    group = snapshots.combine(["one", "two"], "r", "s", root)
    assert len(snapshots.preview(group, "s", root)["changes"]) == 1
    snapshots.restore(group, "s", root)
    assert (root / "file").read_text() == "human baseline"
    assert snapshots.load("one", "s", root)["state"] == "restored"
    assert snapshots.load("two", "s", root)["state"] == "restored"


def test_whole_run_does_not_absorb_interleaved_human_edits(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("baseline")
    first = snapshots.begin(root, "one", "s", "r")
    (root / "file").write_text("agent first")
    snapshots.finish(first)
    (root / "file").write_text("human middle")
    second = snapshots.begin(root, "two", "s", "r")
    (root / "file").write_text("agent second")
    snapshots.finish(second)
    with pytest.raises(SnapshotError, match="interleaved"):
        snapshots.combine(["one", "two"], "r", "s", root)
    assert (root / "file").read_text() == "agent second"


@pytest.mark.asyncio
async def test_execution_checkpoint_and_duplicate_never_recapture(tmp_path, monkeypatch):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("base")
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "executions.db").as_posix())
    await db.init()
    settings = Settings(_env_file=None, computer_enabled=True, workspaces_dir=str(root.parent))
    effects = []
    async def effect(*args, **kwargs):
        assert snapshots.load("e", "s", root)["state"] == "prepared"
        (root / "file").write_text("agent")
        effects.append("once")
        return '{"ok":true}'
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    try:
        store = ExecutionStore(db._session_factory)
        first = await execute(settings, "s", "run_python", {"code": "synthetic"}, execution_id="e", store=store, snapshots=snapshots)
        assert json.loads(first)["checkpoint_id"] == "e"
        (root / "file").write_text("human edit")
        assert await execute(settings, "s", "run_python", {"code": "synthetic"}, execution_id="e", store=store, snapshots=snapshots) == first
        assert effects == ["once"]
        assert snapshots.preview("e", "s", root)["conflicts"] == ["file"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_baseline_blocks_side_effect(tmp_path, monkeypatch):
    root, snapshots = workspace(tmp_path)
    snapshots.max_file_bytes = 1
    (root / "large").write_text("too much")
    db = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "executions.db").as_posix())
    await db.init()
    effect = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    try:
        result = json.loads(await execute(Settings(_env_file=None, computer_enabled=True, workspaces_dir=str(root.parent)), "s", "run_python", {"code": "x"}, execution_id="e", store=ExecutionStore(db._session_factory), snapshots=snapshots))
        assert result["reason"] == "file_checkpoint_unavailable"
        effect.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restore_api_needs_local_confirmation_and_stopped_task(tmp_path):
    root, snapshots = workspace(tmp_path)
    (root / "file").write_text("base")
    value = snapshots.begin(root, "e", "s", "r")
    (root / "file").write_text("agent")
    snapshots.finish(value)
    active = True
    app = FastAPI()
    app.include_router(router)
    app.state.velora = SimpleNamespace(settings=Settings(_env_file=None, workspaces_dir=str(root.parent)),
        store=SimpleNamespace(get_session=AsyncMock(return_value=object())),
        router=SimpleNamespace(snapshots=snapshots, locks=SessionLockManager(), is_running=lambda _: active))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://test") as client:
        assert (await client.post("/api/files/checkpoints/e/restore", json={"session_id": "s", "confirm": True})).status_code == 409
        active = False
        assert (await client.post("/api/files/checkpoints/e/restore", json={"session_id": "s", "confirm": False})).status_code == 409
        assert (await client.get("/api/files/checkpoints/e/changes?session_id=s")).status_code == 200
        assert (await client.post("/api/files/checkpoints/e/restore", json={"session_id": "s", "confirm": True}, headers={"Origin": "http://site"})).status_code == 403
        assert (await client.post("/api/files/checkpoints/e/restore", json={"session_id": "s", "confirm": True})).status_code == 200
        assert (root / "file").read_text() == "base"
