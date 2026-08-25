"""Strip Qwen distill <think>…</think> blocks from assistant text."""

from __future__ import annotations

import re

_THINK_RE = re.compile(
    r"<think>\s*.*?\s*</think>\s*",
    re.DOTALL | re.IGNORECASE,
)
_OPEN_THINK_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_CLOSE_THINK = "</think>"


def strip_think(text: str) -> str:
    """Remove closed think blocks; if an open tag remains, drop from there."""
    if not text:
        return text
    cleaned = _THINK_RE.sub("", text)
    match = _OPEN_THINK_RE.search(cleaned)
    if match:
        cleaned = cleaned[: match.start()]
    return cleaned.strip()


class ThinkStreamFilter:
    """Streaming filter that yields only non-think content.

    Buffers while inside <think>…</think> (including incomplete tags).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._in_think:
                close_idx = self._buf.lower().find(_CLOSE_THINK)
                if close_idx < 0:
                    # Keep a short tail in case close tag is split across chunks.
                    if len(self._buf) > len(_CLOSE_THINK):
                        self._buf = self._buf[-(len(_CLOSE_THINK) - 1) :]
                    break
                end = close_idx + len(_CLOSE_THINK)
                self._buf = self._buf[end:]
                self._in_think = False
                continue

            open_match = _OPEN_THINK_RE.search(self._buf)
            if open_match is None:
                # May hold a partial "<think" prefix.
                hold = _partial_open_prefix(self._buf)
                if hold:
                    safe = self._buf[: -hold]
                    if safe:
                        out.append(safe)
                    self._buf = self._buf[-hold:]
                else:
                    out.append(self._buf)
                    self._buf = ""
                break

            if open_match.start() > 0:
                out.append(self._buf[: open_match.start()])
            self._buf = self._buf[open_match.end() :]
            self._in_think = True

        return "".join(out)

    def flush(self) -> str:
        """Flush remaining safe text (drops unfinished think tail)."""
        if self._in_think:
            self._buf = ""
            return ""
        hold = _partial_open_prefix(self._buf)
        if hold:
            text = self._buf[:-hold]
            self._buf = self._buf[-hold:]
            return text
        text = self._buf
        self._buf = ""
        return text


def _partial_open_prefix(buf: str) -> int:
    """Bytes at end that might be a prefix of an opening <think> tag."""
    lower = buf.lower()
    needle = "<think"
    for i in range(1, min(len(needle), len(lower)) + 1):
        if lower.endswith(needle[:i]):
            return i
    if lower.endswith("<"):
        return 1
    return 0
