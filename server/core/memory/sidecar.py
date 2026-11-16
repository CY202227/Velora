"""Start and own the local memory dependency, or reuse a healthy service."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from server.config import Settings

logger = logging.getLogger("velora.sidecar")

_ROOT = Path(__file__).resolve().parents[3]
_PRESETS = _ROOT / "presets"


@dataclass
class MemorySidecar:
    base_url: str = ""
    started_by_us: bool = False
    _proc: subprocess.Popen[Any] | None = field(default=None, repr=False)

    def stop(self) -> None:
        if not self.started_by_us or self._proc is None:
            return
        if self._proc.poll() is not None:
            self._proc = None
            self.started_by_us = False
            return
        logger.info("stopping atom-memory sidecar pid=%s", self._proc.pid)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)
        self._proc = None
        self.started_by_us = False


def load_preset(preset_id: str) -> dict[str, Any]:
    path = _PRESETS / f"{preset_id}.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def health_ok(base_url: str, *, timeout: float = 1.5) -> bool:
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/health", timeout=timeout, trust_env=not _is_loopback(base_url))
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def atom_memory_root(project_dir: str = "") -> Path | None:
    candidates = [Path(project_dir).resolve()] if project_dir else []
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (candidate / "atom_memory").is_dir():
            return candidate
    if project_dir:
        raise RuntimeError("configured atom-memory project directory does not contain the package")
    return None


def atom_memory_importable(project_dir: str = "") -> bool:
    if atom_memory_root(project_dir) is not None:
        return True
    try:
        import atom_memory.main  # noqa: F401

        return True
    except ImportError:
        return False


def start_memory_sidecar(settings: Settings) -> MemorySidecar:
    """A failed managed dependency prevents default startup; explicit degraded tests may opt out."""
    sidecar = MemorySidecar(base_url=settings.atom_memory_base_url.rstrip("/"))
    if not settings.start_memory_sidecar:
        return sidecar

    preset = load_preset(settings.preset_id)
    sc_cfg = (preset.get("sidecar") or {}) if preset else {}
    if preset and sc_cfg.get("enabled") is False:
        logger.info("preset %s disables sidecar", settings.preset_id)
        return sidecar

    base = settings.atom_memory_base_url.rstrip("/")
    sidecar.base_url = base
    if not _is_loopback(base):
        logger.warning(
            "start_memory_sidecar ignored: atom_memory_base_url is not loopback (%s)",
            base,
        )
        return sidecar

    if health_ok(base):
        logger.info("atom-memory already healthy at %s — skip spawn", base)
        return sidecar

    if not atom_memory_importable(settings.atom_memory_project_dir):
        if settings.memory_required:
            raise RuntimeError('atom-memory dependency unavailable; configure its local project directory or install ".[memory]"')
        return sidecar

    address = urlparse(base)
    if address.scheme != "http" or address.path not in ("", "/"):
        raise RuntimeError("automatic local memory startup requires a plain HTTP root URL")
    host = address.hostname
    port = address.port or 80
    app = str(sc_cfg.get("app") or "atom_memory.main:app")
    db_url = settings.atom_memory_database_url
    sync_llm = bool(sc_cfg.get("sync_llm_to_memory", True))

    (_ROOT / "velora_data").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ATOMMEM_DATABASE_URL"] = db_url
    if sync_llm:
        env["ATOMMEM_LLM_BASE_URL"] = settings.llm_base_url
        env["ATOMMEM_LLM_API_KEY"] = settings.llm_api_key or "EMPTY"
        env["ATOMMEM_LLM_MODEL"] = settings.llm_model
    if settings.atom_memory_api_key:
        env["ATOMMEM_API_KEY"] = settings.atom_memory_api_key
    refs_root = atom_memory_root(settings.atom_memory_project_dir)
    python_exe = sys.executable
    if refs_root is not None:
        env["PYTHONPATH"] = str(refs_root)
        logger.info("atom-memory sidecar using local checkout %s", refs_root)
        # Prefer the checkout's own venv so deps match atom-memory.
        for candidate in (
            refs_root / ".venv" / "Scripts" / "python.exe",
            refs_root / ".venv" / "bin" / "python",
        ):
            if candidate.is_file():
                python_exe = str(candidate)
                logger.info("atom-memory sidecar python=%s", python_exe)
                break

    cmd = [
        python_exe,
        "-m",
        "uvicorn",
        app,
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    logger.info("spawning atom-memory sidecar: %s", " ".join(cmd))
    log_path = Path(settings.atom_memory_sidecar_log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab", buffering=0) as log_f:
            proc = subprocess.Popen(cmd, cwd=str(_ROOT), env=env, stdout=log_f, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:
        logger.exception("failed to spawn atom-memory sidecar")
        if settings.memory_required:
            raise RuntimeError(f"could not start atom-memory dependency; inspect {log_path}")
        return sidecar

    sidecar._proc = proc
    sidecar.started_by_us = True
    deadline = time.monotonic() + settings.atom_memory_startup_timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logger.error(
                "atom-memory sidecar exited early with code %s", proc.returncode
            )
            sidecar._proc = None
            sidecar.started_by_us = False
            if settings.memory_required:
                raise RuntimeError(f"atom-memory dependency exited during startup; inspect {log_path}")
            return sidecar
        if health_ok(base, timeout=1.0):
            logger.info("atom-memory sidecar ready at %s", base)
            return sidecar
        time.sleep(0.25)

    logger.warning("atom-memory sidecar spawn timed out waiting for /health")
    sidecar.stop()
    if settings.memory_required:
        raise RuntimeError(f"atom-memory dependency startup timed out; inspect {log_path}")
    return sidecar
