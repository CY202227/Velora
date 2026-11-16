"""Translate a local workspace launch to the native Windows process boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import hashlib
from pathlib import Path


def request_policy(root: Path, argv: list[str], env: dict[str, str], timeout: int, permissions=None) -> dict:
    if permissions and permissions.host_access:
        raise ValueError("host execution must not be represented as a sandbox policy")
    readonly = sorted({str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve())})
    if permissions:
        readonly = sorted(set(readonly) | set(permissions.read_paths))
    denied = [str(root / name) for name in (".git", ".agents", ".codex") if (root / name).exists()]
    child_env = {**env, "TEMP": str(root), "TMP": str(root), "HOME": str(root),
                 "LOCALAPPDATA": str(root), "USERPROFILE": str(root)}
    return {
        "version": "1.0.0", "containment": "processcontainer",
        "fallback": {"allowDaclMutation": False},
        "filesystem": {"readwritePaths": [str(root)], "readonlyPaths": readonly,
                       "deniedPaths": denied},
        "network": {"egress": {"default": "allow" if permissions and permissions.public_network else "deny"},
                    "ingress": {"default": "deny", "hostLoopback": "deny"}},
        "telemetry": {"enabled": False},
        # Shell initialization needs Win32k; cross-job UI and input stay restricted.
        "ui": {"disable": False, "clipboard": "none", "injection": False},
        "processContainer": {"ui": {"isolation": "container", "desktopSystemControl": False,
                                    "systemSettings": "none", "ime": False}},
        "process": {"commandLine": subprocess.list2cmdline(argv), "cwd": str(root),
                    "env": [f"{key}={value}" for key, value in child_env.items()],
                    "inheritDefaultEnv": False, "timeout": max(1, timeout) * 1000},
        "lifecycle": {"destroyOnExit": True, "preservePolicy": False},
    }


def create_launch(settings, root: Path, argv: list[str], env: dict[str, str], permissions=None):
    from server.core.execution.sandbox import Launch, SandboxUnavailable
    executable = Path(settings.execution_windows_runtime).resolve()
    if executable.is_relative_to(root):
        raise SandboxUnavailable("Windows runtime must be outside the writable workspace")
    if not executable.is_file() or executable.name.lower() != "wxc-exec.exe":
        raise SandboxUnavailable("Windows process runtime is not installed; run scripts/install_windows_sandbox.py")
    try:
        installation = json.loads((executable.parent / "installation.json").read_text(encoding="utf-8"))
        if (not isinstance(installation, dict) or installation["version"] != "1.0.1"
                or not isinstance(installation["files"], dict) or "wxc-exec.exe" not in installation["files"]):
            raise ValueError("invalid runtime installation version")
        for name, expected in installation["files"].items():
            if Path(name).name != name or name in (".", ".."):
                raise ValueError("invalid runtime manifest path")
            with (executable.parent / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("runtime file integrity mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SandboxUnavailable("Windows runtime installation failed integrity verification") from exc
    policy_dir = Path(settings.execution_policy_dir).resolve()
    if policy_dir == root or policy_dir.is_relative_to(root):
        raise SandboxUnavailable("execution policy storage must be outside the writable workspace")
    policy_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="request-", suffix=".json", dir=policy_dir)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(request_policy(root, argv, env, settings.computer_timeout_seconds, permissions), stream)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Launch((str(executable), str(path)), root, env, "native-windows",
                  probe_argv=(str(executable), "--probe", str(path)), policy_file=path)


def validate_probe(payload: dict) -> None:
    from server.core.execution.sandbox import SandboxUnavailable
    if not isinstance(payload, dict) or not isinstance(payload.get("probes"), dict):
        raise SandboxUnavailable("invalid native Windows probe response")
    if (payload.get("error") or payload.get("tier") != "base-container"
            or payload.get("needsDaclAugmentation") is not False
            or payload.get("probes", {}).get("baseContainerSupportsDenyPaths") is not True):
        raise SandboxUnavailable("native Windows policy is unsupported; fallback and host ACL changes are disabled")
    ui = payload.get("probes", {}).get("uiCapabilities", {})
    if not isinstance(ui, dict) or not all(ui.get(key) is True for key in (
        "canBlockClipboardRead", "canBlockClipboardWrite", "canBlockInputInjection",
        "canBlockExternalUiObjects", "canBlockGlobalUiNamespace", "canBlockSystemParameterChanges",
    )):
        raise SandboxUnavailable("native Windows UI restrictions cannot be enforced")
