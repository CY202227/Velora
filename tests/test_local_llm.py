"""Local LLM think-strip, manifest, and sidecar/service mocks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from server.config import Settings
from server.core.local_llm import manifest as manifest_mod
from server.core.local_llm.download import DownloadProgress
from server.core.local_llm.service import LocalLlmService
from server.core.local_llm.sidecar import assets_ready, start_local_llm_sidecar
from server.core.local_llm.think import ThinkStreamFilter, strip_think
from server.core.provider.openai_compat import OpenAICompatProvider


def test_strip_think_closed_and_unclosed() -> None:
    assert strip_think("hello") == "hello"
    assert (
        strip_think("<think>\nplan\n</think>\nanswer") == "answer"
    )
    assert strip_think("before <think>secret</think> after") == "before after"
    assert strip_think("<think>still thinking") == ""
    assert strip_think("visible <think>hidden") == "visible"


def test_think_stream_filter_chunks() -> None:
    filt = ThinkStreamFilter()
    parts = [
        filt.feed("<thi"),
        filt.feed("nk>hidden"),
        filt.feed("</think>"),
        filt.feed(" ok"),
    ]
    assert "".join(parts) == " ok"
    assert filt.flush() == ""


def test_think_stream_filter_partial_open_then_text() -> None:
    filt = ThinkStreamFilter()
    assert filt.feed("hi <") == "hi "
    assert filt.feed("not-a-tag") == "<not-a-tag"
    assert filt.flush() == ""


def test_manifest_has_qwen_gguf() -> None:
    m = manifest_mod.default_manifest()
    assert m["gguf_filename"] == "Qwen3.8-4B-Q4_K_M.gguf"
    assert "empero-ai" in str(m["hf_repo"])
    assert int(m["gguf_expected_bytes"]) > 2_000_000_000  # type: ignore[arg-type]
    asset = manifest_mod.llama_server_asset()
    assert "llama-server" in asset.binary_name or asset.binary_name.endswith(".exe")
    assert "b10615" in asset.url or "llama-" in asset.url


def test_assets_ready_false_when_missing(tmp_path: Path) -> None:
    settings = Settings(
        models_dir=str(tmp_path / "models"),
        bin_dir=str(tmp_path / "bin"),
    )
    assert assets_ready(settings) is False


def test_start_sidecar_skips_without_assets(tmp_path: Path) -> None:
    settings = Settings(
        models_dir=str(tmp_path / "models"),
        bin_dir=str(tmp_path / "bin"),
        start_local_llm_sidecar=True,
        local_llm_base_url="http://127.0.0.1:8040/v1",
    )
    with patch(
        "server.core.local_llm.sidecar.health_ok", return_value=False
    ):
        sc = start_local_llm_sidecar(settings, require_assets=True)
        assert sc.started_by_us is False
        assert sc._proc is None


def test_start_sidecar_skips_when_healthy(tmp_path: Path) -> None:
    settings = Settings(
        models_dir=str(tmp_path / "models"),
        bin_dir=str(tmp_path / "bin"),
        local_llm_base_url="http://127.0.0.1:8040/v1",
    )
    with patch(
        "server.core.local_llm.sidecar.health_ok", return_value=True
    ) as health:
        sc = start_local_llm_sidecar(settings)
        health.assert_called()
        assert sc.started_by_us is False


def test_try_autostart_no_download(tmp_path: Path) -> None:
    settings = Settings(
        models_dir=str(tmp_path / "models"),
        bin_dir=str(tmp_path / "bin"),
        start_local_llm_sidecar=True,
        local_llm_enabled=True,
    )
    provider = OpenAICompatProvider("https://api.openai.com/v1", "")
    svc = LocalLlmService(settings=settings, provider=provider)
    with patch(
        "server.core.local_llm.service.start_local_llm_sidecar"
    ) as spawn:
        svc.try_autostart()
        spawn.assert_not_called()


@pytest.mark.asyncio
async def test_enable_switches_provider(tmp_path: Path) -> None:
    settings = Settings(
        models_dir=str(tmp_path / "models"),
        bin_dir=str(tmp_path / "bin"),
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key="sk-test",
        local_llm_base_url="http://127.0.0.1:8040/v1",
        local_llm_model="qwen3.8-4b-distill",
    )
    provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key)
    svc = LocalLlmService(settings=settings, provider=provider)

    store = MagicMock()

    async def _set(key: str, value: str) -> None:
        return None

    async def _get(key: str) -> str | None:
        return None

    store.set_setting = _set
    store.get_setting = _get

    fake_sc = MagicMock()
    fake_sc.started_by_us = True
    fake_sc.pid = 42

    with (
        patch(
            "server.core.local_llm.service.ensure_assets",
            return_value=(tmp_path / "m.gguf", tmp_path / "llama-server"),
        ),
        patch(
            "server.core.local_llm.service.start_local_llm_sidecar",
            return_value=fake_sc,
        ),
        patch("server.core.local_llm.service.health_ok", return_value=True),
        patch("server.core.local_llm.service.save_settings") as save,
    ):
        status = await svc.enable(store)
        assert status["busy"] is True
        assert svc._task is not None
        await svc._task
        assert settings.local_llm_enabled is True
        assert settings.llm_base_url.endswith(":8040/v1")
        assert settings.llm_model == "qwen3.8-4b-distill"
        assert provider.use_local_sampling is True
        assert svc.status()["enabled"] is True
        save.assert_awaited()


def test_download_progress_cancel() -> None:
    p = DownloadProgress(status="downloading")
    p.cancel()
    assert p.cancelled()
