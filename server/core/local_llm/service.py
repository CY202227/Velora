"""Enable/disable local LLM: download, spawn, cloud snapshot switch."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

from server.config import Settings
from server.core.local_llm import manifest as manifest_mod
from server.core.local_llm.download import DownloadProgress
from server.core.local_llm.sidecar import (
    LocalLlmSidecar,
    assets_ready,
    ensure_assets,
    health_ok,
    start_local_llm_sidecar,
)
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.settings_store import save_settings

logger = logging.getLogger("velora.local_llm.service")

_CLOUD_BASE = "cloud_llm_base_url"
_CLOUD_MODEL = "cloud_llm_model"
_CLOUD_KEY = "cloud_llm_api_key"


@dataclass
class LocalLlmService:
    settings: Settings
    provider: OpenAICompatProvider
    sidecar: LocalLlmSidecar = field(default_factory=LocalLlmSidecar)
    progress: DownloadProgress = field(default_factory=DownloadProgress)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _busy: bool = False
    _task: asyncio.Task | None = field(default=None, repr=False)
    _store: object | None = field(default=None, repr=False)

    def status(self) -> dict[str, object]:
        ready = assets_ready(self.settings)
        running = health_ok(self.settings.local_llm_base_url)
        return {
            "enabled": self.settings.local_llm_enabled,
            "ready": ready,
            "running": running,
            "pid": self.sidecar.pid if self.sidecar.started_by_us else None,
            "busy": self._busy,
            "download": self.progress.to_dict(),
            "model": self.settings.local_llm_model,
            "base_url": self.settings.local_llm_base_url,
            "gguf_expected_bytes": manifest_mod.GGUF_EXPECTED_BYTES,
            "manifest": {
                "hf_repo": manifest_mod.HF_REPO,
                "gguf_filename": manifest_mod.GGUF_FILENAME,
                "llama_note": "Needs recent llama.cpp with Qwen3.5 support",
            },
        }

    def cancel_download(self) -> dict[str, object]:
        self.progress.cancel()
        return self.status()

    async def enable(self, store) -> dict[str, object]:
        """Start enable in the background; return immediately for UI polling."""
        with self._lock:
            if self._busy:
                return self.status()
            self._busy = True
            self._store = store
            self.progress = DownloadProgress()
            self.progress.status = "downloading"
            self.progress.label = manifest_mod.GGUF_FILENAME

        self._task = asyncio.create_task(self._enable_job(store))
        return self.status()

    async def disable(self, store) -> dict[str, object]:
        # Cancel in-flight enable/download if any.
        self.progress.cancel()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

        with self._lock:
            if self._busy and task is None:
                raise RuntimeError("local LLM operation already in progress")
            self._busy = True
        try:
            await self._disable_locked(store)
            return self.status()
        finally:
            self._busy = False

    async def _enable_job(self, store) -> None:
        try:
            await self._enable_locked(store)
        except asyncio.CancelledError:
            self.progress.status = "cancelled"
            self.progress.error = "download cancelled"
            raise
        except Exception as exc:
            logger.exception("local llm enable failed")
            if self.progress.status != "cancelled":
                self.progress.status = "error"
                self.progress.error = str(exc)
        finally:
            self._busy = False
            self._task = None

    async def _enable_locked(self, store) -> None:
        s = self.settings
        # Snapshot current cloud config if we are not already on local.
        if not s.local_llm_enabled and not _looks_local(s.llm_base_url, s):
            await store.set_setting(_CLOUD_BASE, s.llm_base_url)
            await store.set_setting(_CLOUD_MODEL, s.llm_model)
            await store.set_setting(_CLOUD_KEY, s.llm_api_key)

        try:
            await asyncio.to_thread(ensure_assets, s, progress=self.progress)
            self.sidecar.stop()
            self.sidecar = await asyncio.to_thread(
                start_local_llm_sidecar, s, require_assets=True
            )
        except Exception as exc:
            if self.progress.status != "cancelled":
                self.progress.status = "error"
                self.progress.error = str(exc)
            raise

        if not health_ok(s.local_llm_base_url):
            raise RuntimeError("llama-server failed to become healthy")

        s.local_llm_enabled = True
        s.llm_base_url = s.local_llm_base_url.rstrip("/")
        s.llm_model = s.local_llm_model
        # Local server usually ignores API keys; keep empty to avoid leaking cloud key.
        s.llm_api_key = ""
        self.provider.base_url = s.llm_base_url
        self.provider.api_key = s.llm_api_key
        self.provider.use_local_sampling = True
        await save_settings(store, s)
        self.progress.status = "ready"
        self.progress.error = None

    async def _disable_locked(self, store) -> None:
        s = self.settings
        self.sidecar.stop()
        self.sidecar = LocalLlmSidecar(base_url=s.local_llm_base_url)

        cloud_base = await store.get_setting(_CLOUD_BASE)
        cloud_model = await store.get_setting(_CLOUD_MODEL)
        cloud_key = await store.get_setting(_CLOUD_KEY)
        if cloud_base:
            s.llm_base_url = cloud_base.rstrip("/")
        if cloud_model:
            s.llm_model = cloud_model
        if cloud_key is not None:
            s.llm_api_key = cloud_key

        s.local_llm_enabled = False
        self.provider.base_url = s.llm_base_url
        self.provider.api_key = s.llm_api_key
        self.provider.use_local_sampling = False
        await save_settings(store, s)

    def try_autostart(self) -> None:
        """Lifespan: spawn if configured and assets already present (no download)."""
        s = self.settings
        if not (s.start_local_llm_sidecar or s.local_llm_enabled):
            return
        if not assets_ready(s):
            logger.info(
                "start_local_llm_sidecar skipped: GGUF/llama-server not downloaded yet"
            )
            return
        try:
            self.sidecar = start_local_llm_sidecar(s, require_assets=True)
            if s.local_llm_enabled:
                s.llm_base_url = s.local_llm_base_url.rstrip("/")
                s.llm_model = s.local_llm_model
                self.provider.base_url = s.llm_base_url
                self.provider.use_local_sampling = True
        except Exception:
            logger.exception("failed to autostart local llm sidecar")


def _looks_local(base_url: str, settings: Settings) -> bool:
    local = settings.local_llm_base_url.rstrip("/").lower()
    cur = base_url.rstrip("/").lower()
    return cur == local or ":8040" in cur
