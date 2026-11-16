import json
from pathlib import Path
from unittest.mock import patch

import pytest

from server.config import Settings
from server.core.computer.workspace import WorkspaceError, workspace_root
from server.core.execution.sandbox import SandboxUnavailable, build_launch, clean_environment
from server.core.execution.service import FILE_PROGRAM, execute


def config(tmp_path, **kw):
    return Settings(_env_file=None, workspaces_dir=str(tmp_path), computer_enabled=True, **kw)


def test_server_never_uses_native_host(tmp_path):
    with pytest.raises(SandboxUnavailable, match="server execution"):
        build_launch(config(tmp_path, execution_location="server", execution_backend="native"), tmp_path, ["python3"])


def test_container_has_no_host_network_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    with patch("server.core.execution.sandbox.shutil.which", return_value="/trusted/docker"):
        launch = build_launch(config(tmp_path, execution_location="server"), tmp_path, ["python3", "-c", "print(1)"])
    assert "--network=none" in launch.argv
    assert "--cap-drop=ALL" in launch.argv
    assert "--read-only" in launch.argv
    assert "--pull=never" in launch.argv
    assert "secret" not in str(launch)
    assert "OPENAI_API_KEY" not in clean_environment()
    assert launch.argv[-3:] == ("python:3.11-slim", "-c", "print(1)")


def test_local_container_is_separate_from_location(tmp_path):
    with patch("server.core.execution.sandbox.shutil.which", return_value="/trusted/docker"):
        launch = build_launch(config(tmp_path, execution_backend="container"), tmp_path, ["bash"])
    assert launch.backend == "container"


def test_native_exposes_only_selected_roots(tmp_path):
    with patch.object(Path, "exists", return_value=True), patch.object(Path, "is_symlink", return_value=False), patch("server.core.execution.sandbox.sys.platform", "linux"), patch(
        "server.core.execution.sandbox.shutil.which", return_value="/usr/bin/bwrap"
    ):
        launch = build_launch(config(tmp_path), tmp_path, ["python3"])
    assert "--unshare-all" in launch.argv
    assert "--ro-bind" in launch.argv
    assert "/workspace" in launch.argv
    assert ("--ro-bind", "/", "/") not in tuple(zip(launch.argv, launch.argv[1:], launch.argv[2:]))


@pytest.mark.asyncio
async def test_missing_sandbox_never_spawns_workload(tmp_path):
    with patch("server.core.execution.sandbox.shutil.which", return_value=None), patch(
        "server.core.execution.service.asyncio.create_subprocess_exec"
    ) as spawn:
        result = json.loads(await execute(config(tmp_path, execution_backend="container"), "s1", "run_python", {"code": "print(1)"}))
    assert result["reason"] == "sandbox_unavailable"
    spawn.assert_not_called()


@pytest.mark.parametrize("sid", [".", "..", "../escape", "", "a/b"])
def test_workspace_identifier_rejects_escape(tmp_path, sid):
    with pytest.raises(WorkspaceError):
        workspace_root(tmp_path, sid)


def test_workspace_symlink_escape(tmp_path):
    outside = tmp_path.parent
    try:
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(WorkspaceError):
        workspace_root(tmp_path, "escape")


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", ".git/config"])
def test_file_worker_denies_escape_and_metadata(tmp_path, path):
    program = FILE_PROGRAM.replace("pathlib.Path('/workspace')", f"pathlib.Path({str(tmp_path)!r})")
    import io
    with patch("sys.stdin", io.StringIO(json.dumps({"operation": "read_file", "path": path}))):
        with pytest.raises(ValueError):
            exec(program, {})
