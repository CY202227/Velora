"""Pinned download manifest for GGUF + llama-server (Qwen3.5-capable)."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from typing import Literal

# llama.cpp b10615+ required for Qwen3.5 / Gated DeltaNet (this distill model).
_LLAMA_TAG = "b10615"
_LLAMA_BASE = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{_LLAMA_TAG}"
)

HF_REPO = "empero-ai/Qwen3.8-4B-Distill-GGUF"
GGUF_FILENAME = "Qwen3.8-4B-Q4_K_M.gguf"
GGUF_URL = (
    f"https://huggingface.co/{HF_REPO}/resolve/main/{GGUF_FILENAME}"
)
# Size reported by Hugging Face tree API (bytes).
GGUF_EXPECTED_BYTES = 2_783_446_304

MODEL_ALIAS = "qwen3.8-4b-distill"

# Model-card sampling defaults for this distill checkpoint.
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TOP_P = 0.95
DEFAULT_TOP_K = 20


PlatformKey = Literal["win_x64", "win_arm64", "linux_x64", "macos_arm64", "macos_x64"]


@dataclass(frozen=True)
class LlamaServerAsset:
    """Archive that contains llama-server for one OS/arch."""

    platform_key: PlatformKey
    archive_name: str
    url: str
    # Relative path inside the extracted tree (best-effort; search if missing).
    binary_name: str


_ASSETS: dict[PlatformKey, LlamaServerAsset] = {
    "win_x64": LlamaServerAsset(
        platform_key="win_x64",
        archive_name=f"llama-{_LLAMA_TAG}-bin-win-cpu-x64.zip",
        url=f"{_LLAMA_BASE}/llama-{_LLAMA_TAG}-bin-win-cpu-x64.zip",
        binary_name="llama-server.exe",
    ),
    "win_arm64": LlamaServerAsset(
        platform_key="win_arm64",
        archive_name=f"llama-{_LLAMA_TAG}-bin-win-cpu-arm64.zip",
        url=f"{_LLAMA_BASE}/llama-{_LLAMA_TAG}-bin-win-cpu-arm64.zip",
        binary_name="llama-server.exe",
    ),
    "linux_x64": LlamaServerAsset(
        platform_key="linux_x64",
        archive_name=f"llama-{_LLAMA_TAG}-bin-ubuntu-x64.tar.gz",
        url=f"{_LLAMA_BASE}/llama-{_LLAMA_TAG}-bin-ubuntu-x64.tar.gz",
        binary_name="llama-server",
    ),
    "macos_arm64": LlamaServerAsset(
        platform_key="macos_arm64",
        archive_name=f"llama-{_LLAMA_TAG}-bin-macos-arm64.tar.gz",
        url=f"{_LLAMA_BASE}/llama-{_LLAMA_TAG}-bin-macos-arm64.tar.gz",
        binary_name="llama-server",
    ),
    "macos_x64": LlamaServerAsset(
        platform_key="macos_x64",
        archive_name=f"llama-{_LLAMA_TAG}-bin-macos-x64.tar.gz",
        url=f"{_LLAMA_BASE}/llama-{_LLAMA_TAG}-bin-macos-x64.tar.gz",
        binary_name="llama-server",
    ),
}


def detect_platform() -> PlatformKey:
    """Map current OS/arch to a pinned llama-server asset key."""
    system = sys.platform
    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    if system == "win32":
        return "win_arm64" if is_arm else "win_x64"
    if system == "darwin":
        return "macos_arm64" if is_arm else "macos_x64"
    if system.startswith("linux"):
        if is_arm:
            raise RuntimeError(
                "No pinned llama-server asset for Linux ARM in Velora v0; "
                "use x64 or place llama-server under velora_data/bin/"
            )
        return "linux_x64"
    raise RuntimeError(f"Unsupported platform for local LLM: {system}/{machine}")


def llama_server_asset(platform_key: PlatformKey | None = None) -> LlamaServerAsset:
    key = platform_key or detect_platform()
    return _ASSETS[key]


def default_manifest() -> dict[str, object]:
    """Serializable default manifest (tests / status)."""
    asset = llama_server_asset()
    return {
        "hf_repo": HF_REPO,
        "gguf_filename": GGUF_FILENAME,
        "gguf_url": GGUF_URL,
        "gguf_expected_bytes": GGUF_EXPECTED_BYTES,
        "model_alias": MODEL_ALIAS,
        "llama_tag": _LLAMA_TAG,
        "llama_archive": asset.archive_name,
        "llama_url": asset.url,
        "note": "Requires llama.cpp with Qwen3.5 / Gated DeltaNet support",
    }
