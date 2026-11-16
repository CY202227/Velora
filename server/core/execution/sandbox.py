"""Build isolated launches without granting access to the execution host."""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    backend: str
    container_name: str | None = None
    probe_argv: tuple[str, ...] | None = None
    policy_file: Path | None = None


def clean_environment() -> dict[str, str]:
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
    if sys.platform == "win32":
        approved = [str(Path(sys.executable).resolve().parent), str(Path(sys.base_prefix).resolve())]
        if os.environ.get("SystemRoot"):
            system = Path(os.environ["SystemRoot"])
            approved.extend((str(system / "System32"), str(system / "System32/WindowsPowerShell/v1.0"), str(system)))
        env["PATH"] = os.pathsep.join(dict.fromkeys(approved))
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        for key in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def build_launch(settings, root: Path, argv: list[str], *, permissions=None) -> Launch:
    location = settings.execution_location
    backend = settings.execution_backend
    if backend == "auto":
        backend = "container" if location == "server" else "native"
    if location == "server" and backend != "container":
        raise SandboxUnavailable("server execution requires the container backend")
    env = clean_environment()
    if permissions and permissions.host_access:
        if (location != "local" or backend != "native" or not settings.execution_allow_host
                or not settings.api_auth_enabled):
            raise SandboxUnavailable("host execution is outside administrator policy")
        return Launch(tuple(argv), root, env, "host-approved")
    if backend == "native" and sys.platform == "win32":
        from server.core.execution.windows import create_launch
        return create_launch(settings, root, argv, env, permissions)
    if permissions and permissions.elevated:
        raise SandboxUnavailable("this backend cannot enforce additional permission scopes")
    if backend == "container":
        engine = shutil.which("docker")
        if engine is None:
            raise SandboxUnavailable("container engine is not installed")
        if "," in str(root) or "\n" in str(root):
            raise SandboxUnavailable("workspace path cannot be represented as a container mount")
        name = "velora-exec-" + uuid.uuid4().hex
        command = [engine, "run", "--rm", "--pull=never", "--name", name,
                   "--init", "--interactive", "--network=none", "--read-only",
                   "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=128", "--memory=512m", "--cpus=1",
                   "--user", f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "65534:65534",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=67108864",
                   "--mount", f"type=bind,src={root},dst=/workspace",
                   "--workdir", "/workspace", "--env", "HOME=/tmp",
                   "--env", "PYTHONIOENCODING=utf-8"]
        for metadata in (".git", ".agents", ".codex"):
            path = root / metadata
            if path.exists():
                if path.is_symlink():
                    raise SandboxUnavailable("workspace metadata symlinks are not supported")
                command += ["--mount", f"type=bind,src={path},dst=/workspace/{metadata},readonly"]
        command += ["--entrypoint", argv[0], settings.execution_container_image, *argv[1:]]
        return Launch(tuple(command), root, env, backend, name)
    if backend != "native" or sys.platform != "linux":
        raise SandboxUnavailable("native sandbox is unavailable on this platform; no host fallback")
    wrapper = shutil.which("bwrap")
    if not wrapper:
        raise SandboxUnavailable("native sandbox requires bubblewrap")
    # Expose runtime files, not the host's root, home, sockets or application data.
    command = [wrapper, "--die-with-parent", "--new-session", "--unshare-all",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
               "--dir", "/run", "--dir", "/etc", "--dir", "/home"]
    for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    for path in ("/etc/ld.so.cache", "/etc/localtime"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--bind", str(root), "/workspace"]
    for metadata in (".git", ".agents", ".codex"):
        path = root / metadata
        if path.exists():
            if path.is_symlink():
                raise SandboxUnavailable("workspace metadata symlinks are not supported")
            command += ["--ro-bind", str(path), f"/workspace/{metadata}"]
    command += ["--chdir", "/workspace", "--setenv", "HOME", "/tmp", "--", *argv]
    return Launch(tuple(command), root, env, backend)


def sandbox_status(settings) -> dict:
    backend = settings.execution_backend
    if backend == "auto":
        backend = "container" if settings.execution_location == "server" else "native"
    supported = backend == "container" or (backend == "native" and sys.platform in ("linux", "win32"))
    dependency = "docker" if backend == "container" else "bwrap"
    installed = supported and shutil.which(dependency) is not None
    if backend == "native" and sys.platform == "win32":
        installed = Path(settings.execution_windows_runtime).is_file()
    return {"location": settings.execution_location, "backend": backend,
            "configuration_allowed": settings.execution_location != "server" or backend == "container",
            "platform_supported": supported, "dependency_installed": installed,
            "enforcement_verified": False, "host_fallback": False,
            "network": "disabled", "windows_native": (
                "probe_required" if installed else "runtime_missing"
            ) if backend == "native" and sys.platform == "win32" else "not_selected"}
