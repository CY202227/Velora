"""Warmth style overlay + memory sidecar branch tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult, TurnRequest
from server.core.memory import sidecar as sidecar_mod
from server.core.nodes.compose_prompt import ComposePromptNode
from server.core.persona.default import DEFAULT_PERSONA
from server.core.persona.style import (
    clamp_warmth,
    merge_style_knobs,
    warmth_instruction,
)


@pytest.mark.asyncio
async def test_compose_warmth_assistant_vs_companion() -> None:
    base = TurnContext(
        request=TurnRequest("s", "hi"),
        session_id="s",
        persona=DEFAULT_PERSONA,
        style_knobs={"warmth": 10},
    )
    assert await ComposePromptNode().process(base) is NodeResult.CONTINUE
    sys_low = base.messages[0]["content"]
    assert "更偏日常助理" in sys_low
    assert "恋爱对象" in sys_low  # persona baseline

    high = TurnContext(
        request=TurnRequest("s", "hi"),
        session_id="s",
        persona=DEFAULT_PERSONA,
        style_knobs={"warmth": 90},
    )
    assert await ComposePromptNode().process(high) is NodeResult.CONTINUE
    sys_high = high.messages[0]["content"]
    assert "亲密陪伴" in sys_high
    assert "小三" in sys_high
    assert "恋爱对象" in sys_high  # persona baseline still present


def test_warmth_helpers() -> None:
    assert clamp_warmth(-5) == 0
    assert clamp_warmth(200) == 100
    merged = merge_style_knobs(
        {"warmth": "acquaintance"},
        {"warmth": 60},
        default_warmth=35,
    )
    assert merged["warmth"] == 60
    assert "助理" in warmth_instruction(10)
    assert "陪伴" in warmth_instruction(70)


def test_sidecar_skips_when_disabled() -> None:
    settings = Settings(start_memory_sidecar=False)
    sc = sidecar_mod.start_memory_sidecar(settings)
    assert sc.started_by_us is False
    assert sc._proc is None


def test_sidecar_skips_when_already_healthy() -> None:
    settings = Settings(
        start_memory_sidecar=True,
        atom_memory_base_url="http://127.0.0.1:8020",
        preset_id="local-default",
    )
    with patch.object(sidecar_mod, "health_ok", return_value=True) as health:
        with patch.object(sidecar_mod, "atom_memory_importable") as imp:
            sc = sidecar_mod.start_memory_sidecar(settings)
            health.assert_called()
            imp.assert_not_called()
            assert sc.started_by_us is False


def test_sidecar_skips_when_package_missing() -> None:
    settings = Settings(
        start_memory_sidecar=True,
        atom_memory_base_url="http://127.0.0.1:8020",
        preset_id="local-default",
    )
    with patch.object(sidecar_mod, "health_ok", return_value=False):
        with patch.object(sidecar_mod, "atom_memory_importable", return_value=False):
            with patch.object(sidecar_mod.subprocess, "Popen") as popen:
                sc = sidecar_mod.start_memory_sidecar(settings)
                popen.assert_not_called()
                assert sc.started_by_us is False


def test_sidecar_spawns_when_needed() -> None:
    settings = Settings(
        start_memory_sidecar=True,
        atom_memory_base_url="http://127.0.0.1:8020",
        preset_id="local-default",
        llm_base_url="http://example/v1",
        llm_api_key="k",
        llm_model="m",
    )
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.pid = 4242

    with patch.object(sidecar_mod, "health_ok", side_effect=[False, True]):
        with patch.object(sidecar_mod, "atom_memory_importable", return_value=True):
            with patch.object(sidecar_mod.subprocess, "Popen", return_value=fake_proc) as popen:
                with patch.object(sidecar_mod.time, "sleep"):
                    sc = sidecar_mod.start_memory_sidecar(settings)
                    popen.assert_called_once()
                    assert sc.started_by_us is True
                    assert sc._proc is fake_proc
                    env = popen.call_args.kwargs["env"]
                    assert env["ATOMMEM_LLM_MODEL"] == "m"
                    assert env["ATOMMEM_LLM_BASE_URL"] == "http://example/v1"


def test_load_preset_local_default() -> None:
    preset = sidecar_mod.load_preset("local-default")
    assert preset.get("id") == "local-default"
    assert preset["sidecar"]["port"] == 8020
    assert preset["sidecar"]["sync_llm_to_memory"] is True
