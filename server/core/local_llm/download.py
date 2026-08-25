"""Streaming download helpers with .part resume and progress state."""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("velora.local_llm.download")


@dataclass
class DownloadProgress:
    label: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    status: str = "idle"  # idle|downloading|extracting|ready|error|cancelled
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def percent(self) -> float | None:
        if self.bytes_total <= 0:
            return None
        return min(100.0, 100.0 * self.bytes_done / self.bytes_total)

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": self.percent,
            "status": self.status,
            "error": self.error,
        }


def download_file(
    url: str,
    dest: Path,
    *,
    progress: DownloadProgress,
    expected_bytes: int | None = None,
    sha256: str | None = None,
    timeout: float = 600.0,
) -> Path:
    """Download url to dest using dest.with_suffix(dest.suffix + '.part') resume."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    progress.label = dest.name
    progress.status = "downloading"
    progress.error = None
    progress.reset_cancel()

    headers: dict[str, str] = {}
    resume_from = 0
    if part.is_file():
        resume_from = part.stat().st_size
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

    hasher = hashlib.sha256() if sha256 else None
    if hasher and part.is_file() and resume_from:
        with part.open("rb") as existing:
            while True:
                chunk = existing.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:
                # Already complete according to server.
                if part.is_file():
                    part.replace(dest)
                progress.bytes_done = dest.stat().st_size if dest.is_file() else 0
                progress.bytes_total = progress.bytes_done
                progress.status = "ready"
                return dest
            if resp.status_code >= 400:
                progress.status = "error"
                progress.error = f"HTTP {resp.status_code} for {url}"
                raise RuntimeError(progress.error)

            total = expected_bytes
            cr = resp.headers.get("content-range")
            if cr and "/" in cr:
                try:
                    total = int(cr.rsplit("/", 1)[-1])
                except ValueError:
                    pass
            elif resp.headers.get("content-length"):
                try:
                    cl = int(resp.headers["content-length"])
                    total = cl + resume_from if resume_from and resp.status_code == 206 else cl
                except ValueError:
                    pass
            if total:
                progress.bytes_total = total
            progress.bytes_done = resume_from

            mode = "ab" if resp.status_code == 206 and resume_from else "wb"
            if mode == "wb" and part.exists():
                part.unlink()
                resume_from = 0
                progress.bytes_done = 0
                if hasher and sha256:
                    hasher = hashlib.sha256()

            with part.open(mode) as out:
                for chunk in resp.iter_bytes(1024 * 256):
                    if progress.cancelled():
                        progress.status = "cancelled"
                        progress.error = "download cancelled"
                        raise RuntimeError(progress.error)
                    out.write(chunk)
                    if hasher:
                        hasher.update(chunk)
                    progress.bytes_done += len(chunk)

    if sha256 and hasher and hasher.hexdigest().lower() != sha256.lower():
        progress.status = "error"
        progress.error = "sha256 mismatch"
        part.unlink(missing_ok=True)
        raise RuntimeError(progress.error)

    part.replace(dest)
    progress.bytes_done = dest.stat().st_size
    if progress.bytes_total <= 0:
        progress.bytes_total = progress.bytes_done
    progress.status = "ready"
    logger.info("downloaded %s (%s bytes)", dest, progress.bytes_done)
    return dest
