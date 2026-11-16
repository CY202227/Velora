import json
from unittest.mock import AsyncMock, patch

import pytest

from server.config import Settings
from server.core.execution.permissions import resolve, Permissions
from server.core.execution.service import execute
from server.core.execution.windows import request_policy


def test_scopes_need_admin_bounds_and_reason(tmp_path):
    outside = tmp_path / "docs"
    outside.mkdir()
    s = Settings(_env_file=None, execution_read_allowlist=[str(outside)])
    with patch("server.core.execution.permissions.sys.platform", "win32"):
        assert resolve(s, {"permissions": {"read_paths": [str(outside)], "reason": "inspect documents"}}, "read_file").elevated
        for raw in ({"read_paths": [str(tmp_path)], "reason": "too broad"},
                    {"public_network": True, "reason": "network"},
                    {"read_paths": [str(outside)]},
                    {"host_access": True}):
            with pytest.raises(PermissionError):
                resolve(s, {"permissions": raw}, "read_file")


def test_private_state_cannot_be_granted(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    s = Settings(_env_file=None, execution_read_allowlist=[str(tmp_path)], execution_snapshots_dir=str(private))
    with patch("server.core.execution.permissions.sys.platform", "win32"):
        with pytest.raises(PermissionError, match="runtime state"):
            resolve(s, {"permissions": {"read_paths": [str(tmp_path)], "reason": "broad"}}, "run_python")


@pytest.mark.asyncio
async def test_scoped_request_always_requires_confirmation_context(tmp_path, monkeypatch):
    outside = tmp_path / "docs"
    outside.mkdir()
    s = Settings(_env_file=None, computer_enabled=True, execution_read_allowlist=[str(outside)])
    effect = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    result = json.loads(await execute(s, "s", "read_file", {"path": str(outside), "permissions": {
        "read_paths": [str(outside)], "reason": "read documentation",
    }}))
    assert result["reason"] == "approval_unavailable"
    effect.assert_not_awaited()


def test_policy_expands_read_and_public_egress_but_not_host_access(tmp_path):
    scope = Permissions((str(tmp_path / "docs"),), True, "test")
    policy = request_policy(tmp_path / "workspace", ["python.exe"], {}, 10, scope)
    assert str(tmp_path / "docs") in policy["filesystem"]["readonlyPaths"]
    assert policy["filesystem"]["readwritePaths"] == [str(tmp_path / "workspace")]
    assert policy["network"]["egress"]["default"] == "allow"
    assert policy["network"]["ingress"] == {"default": "deny", "hostLoopback": "deny"}
    assert policy["fallback"]["allowDaclMutation"] is False


def test_host_access_needs_admin_auth_and_cannot_claim_restricted_scope():
    with patch("server.core.execution.permissions.sys.platform", "win32"):
        args = {"permissions": {"host_access": True, "reason": "trusted local operation"}}
        with pytest.raises(PermissionError):
            resolve(Settings(_env_file=None), args, "run_python")
        scope = resolve(Settings(_env_file=None, execution_allow_host=True), args, "run_python")
        assert scope.host_access and scope.review()["loopback_and_private_network"] == "unrestricted"
        with pytest.raises(PermissionError):
            resolve(Settings(_env_file=None, execution_allow_host=True, api_auth_enabled=False), args, "run_python")
        with pytest.raises(PermissionError):
            resolve(Settings(_env_file=None, execution_allow_host=True), args, "write_file")


@pytest.mark.asyncio
async def test_host_access_without_grant_never_launches(monkeypatch):
    effect = AsyncMock()
    monkeypatch.setattr("server.core.execution.service._execute", effect)
    result = json.loads(await execute(Settings(_env_file=None, computer_enabled=True, execution_allow_host=True), "s", "run_python", {
        "code": "print(1)", "permissions": {"host_access": True, "reason": "trusted code"},
    }))
    assert result["reason"] == "approval_unavailable"
    effect.assert_not_awaited()
