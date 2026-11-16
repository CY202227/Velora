import hashlib
import json
from pathlib import Path

import pytest

from server.config import Settings
from server.core.execution.sandbox import SandboxUnavailable
from server.core.execution.windows import create_launch, request_policy, validate_probe


def test_policy_does_not_inherit_credentials_or_allow_network(tmp_path):
    policy = request_policy(tmp_path, [r"C:\runtime\python.exe", "-c", "print('a b')"], {"SystemRoot": r"C:\Windows"}, 10)
    assert policy["version"] == "1.0.0"
    assert policy["telemetry"] == {"enabled": False}
    assert policy["fallback"] == {"allowDaclMutation": False}
    assert policy["process"]["inheritDefaultEnv"] is False
    assert f"TEMP={tmp_path}" in policy["process"]["env"]
    assert policy["network"]["ingress"]["hostLoopback"] == "deny"
    assert policy["filesystem"]["readwritePaths"] == [str(tmp_path)]
    assert policy["ui"]["clipboard"] == "none"
    assert policy["processContainer"]["ui"]["isolation"] == "container"


@pytest.mark.parametrize("probe", [[], {}, {"probes": []},
    {"tier": "appcontainer-dacl", "probes": {}},
    {"tier": "base-container", "needsDaclAugmentation": True, "probes": {}},
    {"tier": "base-container", "needsDaclAugmentation": False,
     "probes": {"baseContainerSupportsDenyPaths": True, "uiCapabilities": {}}},
])
def test_probe_rejects_weaker_or_malformed_capabilities(probe):
    with pytest.raises(SandboxUnavailable):
        validate_probe(probe)


def installation(tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir()
    exe = directory / "wxc-exec.exe"
    exe.write_bytes(b"synthetic, never executed")
    (directory / "installation.json").write_text(json.dumps({
        "version": "1.0.1", "files": {exe.name: hashlib.sha256(exe.read_bytes()).hexdigest()},
    }))
    return exe


def test_request_is_outside_workspace_and_requires_probe(tmp_path):
    exe = installation(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    s = Settings(_env_file=None, execution_windows_runtime=str(exe), execution_policy_dir=str(tmp_path / "policies"))
    launch = create_launch(s, root, ["python.exe", "-c", "print(1)"], {})
    try:
        assert launch.probe_argv == (str(exe), "--probe", str(launch.policy_file))
        assert not launch.policy_file.is_relative_to(root)
        assert json.loads(launch.policy_file.read_text())["process"]["cwd"] == str(root)
    finally:
        launch.policy_file.unlink()


def test_runtime_tampering_fails_closed(tmp_path):
    exe = installation(tmp_path)
    exe.write_bytes(b"tampered")
    s = Settings(_env_file=None, execution_windows_runtime=str(exe))
    with pytest.raises(SandboxUnavailable, match="integrity"):
        create_launch(s, tmp_path / "workspace", ["python.exe"], {})
