"""Optional atom-memory subprocess for the local-default preset."""

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
        resp = httpx.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
        return resp.status_code < 400
    except Exception:
        return False


def atom_memory_root() -> Path | None:
    """Prefer the local refs checkout so sidecar tracks refs/atom_memory."""
    refs = _ROOT / "refs" / "atom_memory"
    if (refs / "pyproject.toml").is_file() and (refs / "atom_memory").is_dir():
        return refs
    return None


def atom_memory_importable() -> bool:
    if atom_memory_root() is not None:
        return True
    try:
        import atom_memory.main  # noqa: F401

        return True
    except ImportError:
        return False


def start_memory_sidecar(settings: Settings) -> MemorySidecar:
    """Start atom-memory if configured; never raise for missing package."""
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

    if not atom_memory_importable():
        logger.warning(
            "atom-memory not found; clone to refs/atom_memory "
            'or pip install -e ".[memory]"'
        )
        return sidecar

    host = str(sc_cfg.get("host") or "127.0.0.1")
    port = int(sc_cfg.get("port") or urlparse(base).port or 8020)
    app = str(sc_cfg.get("app") or "atom_memory.main:app")
    db_url = str(
        sc_cfg.get("database_url") or "sqlite:///./velora_data/atom_memory.db"
    )
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
    refs_root = atom_memory_root()
    python_exe = sys.executable
    if refs_root is not None:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(refs_root)
            if not existing
            else f"{refs_root}{os.pathsep}{existing}"
        )
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
    log_path = _ROOT / "velora_data" / "atom_memory_sidecar.log"
    try:
        # Keep handle open for process lifetime (closed when Velora exits).
        log_f = open(log_path, "ab", buffering=0)  # noqa: SIM115
        proc = subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        logger.exception("failed to spawn atom-memory sidecar")
        return sidecar

    sidecar._proc = proc
    sidecar.started_by_us = True
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.error(
                "atom-memory sidecar exited early with code %s", proc.returncode
            )
            sidecar._proc = None
            sidecar.started_by_us = False
            return sidecar
        if health_ok(base, timeout=1.0):
            logger.info("atom-memory sidecar ready at %s", base)
            return sidecar
        time.sleep(0.25)

    logger.warning("atom-memory sidecar spawn timed out waiting for /health")
    return sidecar
