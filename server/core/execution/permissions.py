"""Resolve requested exceptions against administrator bounds before confirmation."""
from dataclasses import asdict, dataclass
from pathlib import Path
import stat
import sys
import os


@dataclass(frozen=True)
class Permissions:
    read_paths: tuple[str, ...] = ()
    public_network: bool = False
    reason: str = ""
    host_access: bool = False

    @property
    def elevated(self):
        return bool(self.read_paths or self.public_network or self.host_access)

    def review(self):
        return {**asdict(self), "loopback_and_private_network": "unrestricted" if self.host_access else "denied",
                "file_undo_coverage": "workspace_only",
                "authority": "current OS account; trusted host code" if self.host_access else "sandbox"}


def resolve(settings, arguments, operation):
    raw = arguments.get("permissions", {})
    if not isinstance(raw, dict) or set(raw) - {"read_paths", "public_network", "reason", "host_access"}:
        raise PermissionError("unsupported permission request")
    paths = raw.get("read_paths", [])
    network = raw.get("public_network", False)
    reason = raw.get("reason", "")
    host = raw.get("host_access", False)
    if not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(path, str) for path in paths):
        raise PermissionError("read_paths must contain at most eight absolute local paths")
    if type(network) is not bool or type(host) is not bool or not isinstance(reason, str) or len(reason) > 500:
        raise PermissionError("invalid permission scope")
    if not paths and not network and not host:
        return Permissions()
    if not reason.strip():
        raise PermissionError("additional permissions require an explanation")
    if settings.execution_location != "local" or settings.execution_backend not in ("native", "auto") or sys.platform != "win32":
        raise PermissionError("additional permissions are currently supported only by native local Windows execution")
    if host:
        if not settings.execution_allow_host or not settings.api_auth_enabled:
            raise PermissionError("host execution requires administrator enablement and control API authentication")
        if paths or network or operation not in ("run_shell", "run_python"):
            raise PermissionError("host execution is full account authority, not a restricted read/network scope")
        return Permissions(reason=reason.strip(), host_access=True)
    if network and (not settings.execution_allow_public_network or operation not in ("run_shell", "run_python")):
        raise PermissionError("public network access is outside administrator policy")
    allowed = [Path(path).resolve() for path in settings.execution_read_allowlist]
    protected = [Path(settings.execution_snapshots_dir).resolve(), Path(settings.execution_policy_dir).resolve(),
                  Path(settings.control_token_path).resolve(), Path(settings.execution_output_dir).resolve(),
                  Path(settings.execution_receipts_dir).resolve(),
                  Path(settings.runtime_archive_dir).resolve(),
                 Path(settings.execution_windows_runtime).resolve().parent, Path(settings.workspaces_dir).resolve()]
    from sqlalchemy.engine import make_url
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        protected.append(Path(url.database).resolve())
    resolved = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute() or raw_path.startswith(("\\\\", "//")) or ".." in path.parts:
            raise PermissionError("read permission requires an absolute local path without aliases")
        if os.name == "nt":
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:
                raise PermissionError("network-mounted permission roots are not supported")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("linked permission roots are not supported")
        path = path.resolve(strict=True)
        if not any(path == base or path.is_relative_to(base) for base in allowed):
            raise PermissionError("requested read path is outside administrator allowlist")
        if any(path == private or path.is_relative_to(private) or private.is_relative_to(path) for private in protected):
            raise PermissionError("runtime state and other session workspaces cannot be exposed")
        resolved.append(str(path))
    return Permissions(tuple(sorted(set(resolved))), network, reason.strip())


SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "read_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    "public_network": {"type": "boolean", "description": "仅公网出站；不开放本机或私网"},
    "host_access": {"type": "boolean", "description": "直接以本机账户运行，仅管理员启用且用户单次明确授权后可用；文件检查点只覆盖工作区"},
    "reason": {"type": "string", "maxLength": 500},
}}
