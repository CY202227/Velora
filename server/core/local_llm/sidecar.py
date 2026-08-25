"""llama-server subprocess sidecar for the builtin local model."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from server.config import Settings
from server.core.local_llm import manifest as manifest_mod
from server.core.local_llm.download import DownloadProgress, download_file
from server.core.memory.sidecar import load_preset

logger = logging.getLogger("velora.local_llm.sidecar")


@dataclass
class LocalLlmSidecar:
    base_url: str = ""
    started_by_us: bool = False
    pid: int | None = None
    _proc: subprocess.Popen[Any] | None = field(default=None, repr=False)

    def stop(self) -> None:
        if not self.started_by_us or self._proc is None:
            self.started_by_us = False
            self.pid = None
            self._proc = None
            return
        if self._proc.poll() is not None:
            self._proc = None
            self.started_by_us = False
            self.pid = None
            return
        logger.info("stopping local llm sidecar pid=%s", self._proc.pid)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None
        self.started_by_us = False
        self.pid = None


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def health_ok(base_url: str, *, timeout: float = 2.0) -> bool:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    for path in ("/health", "/v1/models"):
        try:
            resp = httpx.get(f"{root}{path}", timeout=timeout)
            if resp.status_code < 500:
                return True
        except Exception:
            continue
    return False


def gguf_path(settings: Settings) -> Path:
    return Path(settings.models_dir) / manifest_mod.GGUF_FILENAME


def llama_binary_path(settings: Settings) -> Path:
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    return Path(settings.bin_dir) / name


def assets_ready(settings: Settings) -> bool:
    gguf = gguf_path(settings)
    binary = llama_binary_path(settings)
    if not binary.is_file():
        return False
    if not gguf.is_file():
        return False
    # Allow slightly smaller files only if still downloading; require near-complete.
    size = gguf.stat().st_size
    return size >= int(manifest_mod.GGUF_EXPECTED_BYTES * 0.99)


def _extract_llama_archive(archive: Path, bin_dir: Path, binary_name: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    extract_dir = bin_dir / "_llama_extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    if archive.suffix == ".zip" or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(extract_dir)
    else:
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(extract_dir)

    found: Path | None = None
    for path in extract_dir.rglob(binary_name):
        if path.is_file():
            found = path
            break
    if found is None:
        raise RuntimeError(f"{binary_name} not found in {archive.name}")

    dest = bin_dir / binary_name
    shutil.copy2(found, dest)
    if os.name != "nt":
        dest.chmod(dest.stat().st_mode | 0o111)

    # Copy sibling shared libs next to the binary (Windows DLLs, etc.).
    for sibling in found.parent.iterdir():
        if sibling == found or not sibling.is_file():
            continue
        target = bin_dir / sibling.name
        if not target.exists():
            shutil.copy2(sibling, target)

    shutil.rmtree(extract_dir, ignore_errors=True)
    return dest


def ensure_assets(
    settings: Settings,
    *,
    progress: DownloadProgress,
) -> tuple[Path, Path]:
    """Download GGUF + llama-server if missing. Does not spawn the server."""
    Path(settings.models_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.bin_dir).mkdir(parents=True, exist_ok=True)

    gguf = gguf_path(settings)
    if not gguf.is_file() or gguf.stat().st_size < int(
        manifest_mod.GGUF_EXPECTED_BYTES * 0.99
    ):
        download_file(
            manifest_mod.GGUF_URL,
            gguf,
            progress=progress,
            expected_bytes=manifest_mod.GGUF_EXPECTED_BYTES,
        )

    binary = llama_binary_path(settings)
    if not binary.is_file():
        asset = manifest_mod.llama_server_asset()
        archive = Path(settings.bin_dir) / asset.archive_name
        if not archive.is_file():
            download_file(asset.url, archive, progress=progress)
        progress.status = "extracting"
        progress.label = asset.binary_name
        _extract_llama_archive(archive, Path(settings.bin_dir), asset.binary_name)

    progress.status = "ready"
    progress.error = None
    return gguf, binary


def _port_and_ctx(settings: Settings) -> tuple[int, int]:
    port = settings.local_llm_port
    ctx = settings.local_llm_ctx
    preset = load_preset(settings.preset_id)
    ll_cfg = (preset.get("local_llm") or {}) if preset else {}
    if isinstance(ll_cfg.get("port"), int):
        port = ll_cfg["port"]
    if isinstance(ll_cfg.get("ctx"), int):
        ctx = ll_cfg["ctx"]
    return port, ctx


def start_local_llm_sidecar(
    settings: Settings,
    *,
    require_assets: bool = True,
) -> LocalLlmSidecar:
    """Spawn llama-server when assets exist (or require_assets is False and they do)."""
    port, ctx = _port_and_ctx(settings)
    base = settings.local_llm_base_url.rstrip("/")
    sidecar = LocalLlmSidecar(base_url=base)

    if not _is_loopback(base):
        logger.warning("local llm base_url is not loopback (%s)", base)
        return sidecar

    if health_ok(base):
        logger.info("local llm already healthy at %s — skip spawn", base)
        return sidecar

    if not assets_ready(settings):
        if require_assets:
            logger.warning(
                "local llm assets missing under %s / %s — skip spawn",
                settings.models_dir,
                settings.bin_dir,
            )
        return sidecar

    gguf = gguf_path(settings)
    binary = llama_binary_path(settings)
    host = "127.0.0.1"
    alias = settings.local_llm_model or manifest_mod.MODEL_ALIAS
    cmd = [
        str(binary),
        "-m",
        str(gguf),
        "--host",
        host,
        "--port",
        str(port),
        "--alias",
        alias,
        "-c",
        str(ctx),
    ]
    logger.info("starting local llm: %s", " ".join(cmd))
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(settings.bin_dir)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    sidecar._proc = proc
    sidecar.started_by_us = True
    sidecar.pid = proc.pid

    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            sidecar.started_by_us = False
            sidecar.pid = None
            sidecar._proc = None
            raise RuntimeError(
                f"llama-server exited early with code {proc.returncode}"
            )
        if health_ok(base, timeout=1.0):
            logger.info("local llm ready at %s pid=%s", base, proc.pid)
            return sidecar
        time.sleep(0.5)

    sidecar.stop()
    raise RuntimeError("timed out waiting for llama-server /health")
