"""Builtin local LLM: GGUF download + llama-server sidecar."""

from server.core.local_llm.think import strip_think, ThinkStreamFilter

__all__ = ["strip_think", "ThinkStreamFilter"]
