"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from server.core.local_llm.manifest import (
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
)
from server.core.local_llm.think import ThinkStreamFilter, strip_think
from server.core.provider.base import ChatResult, StreamEvent, ToolCall

logger = logging.getLogger(__name__)


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    tool_calls: list[ToolCall] = []
    if not isinstance(raw_calls, list):
        return tool_calls
    for tc in raw_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments")
        if args is None:
            args = "{}"
        elif not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        tool_calls.append(
            ToolCall(
                id=str(tc.get("id") or f"call_{len(tool_calls)}"),
                name=str(name),
                arguments=args,
            )
        )
    return tool_calls


def _merge_tool_call_delta(
    acc: dict[int, dict[str, Any]],
    deltas: Any,
) -> None:
    """Accumulate OpenAI-style streaming tool_calls deltas by index."""
    if not isinstance(deltas, list):
        return
    for i, tc in enumerate(deltas):
        if not isinstance(tc, dict):
            continue
        idx_raw = tc.get("index")
        try:
            idx = int(idx_raw) if idx_raw is not None else i
        except (TypeError, ValueError):
            idx = i
        slot = acc.setdefault(
            idx,
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if tc.get("id"):
            slot["id"] = str(tc["id"])
        if tc.get("type"):
            slot["type"] = str(tc["type"])
        fn = tc.get("function")
        if isinstance(fn, dict):
            if fn.get("name"):
                slot["function"]["name"] = str(fn["name"])
            args = fn.get("arguments")
            if args is not None:
                slot["function"]["arguments"] = (
                    str(slot["function"].get("arguments") or "") + str(args)
                )


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
        strip_think_blocks: bool = True,
        use_local_sampling: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.strip_think_blocks = strip_think_blocks
        self.use_local_sampling = use_local_sampling

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _maybe_sampling(self, payload: dict[str, Any]) -> None:
        if not self.use_local_sampling:
            return
        payload.setdefault("temperature", DEFAULT_TEMPERATURE)
        payload.setdefault("top_p", DEFAULT_TOP_P)
        payload.setdefault("top_k", DEFAULT_TOP_K)

    def _clean(self, text: str | None) -> str | None:
        if text is None:
            return None
        if not self.strip_think_blocks:
            return text
        return strip_think(text)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> str:
        result = await self.chat(messages, model=model)
        return (result.content or "").strip()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ChatResult:
        """Non-streaming completion; optionally with tools / tool_calls."""
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        self._maybe_sampling(payload)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} for {url}: {resp.text}",
                    request=resp.request,
                    response=resp,
                )
            obj = resp.json()
        choices = obj.get("choices") or []
        if not choices:
            return ChatResult()
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            content = str(content)
        content = self._clean(content)
        return ChatResult(
            content=content,
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one chat/completions request; emit deltas then a final ChatResult.

        Tools (if any) are attached to this same request, so a
        plain reply never needs a second generation pass.
        """
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        self._maybe_sampling(payload)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"

        filt = ThinkStreamFilter() if self.strip_think_blocks else None
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                url,
                headers=self._headers(),
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} for {url}: {detail}",
                        request=resp.request,
                        response=resp,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    _merge_tool_call_delta(tool_acc, delta.get("tool_calls"))
                    content = delta.get("content")
                    if not content:
                        continue
                    if filt is None:
                        content_parts.append(content)
                        yield StreamEvent(delta=content)
                    else:
                        piece = filt.feed(content)
                        if piece:
                            content_parts.append(piece)
                            yield StreamEvent(delta=piece)
                if filt is not None:
                    tail = filt.flush()
                    if tail:
                        content_parts.append(tail)
                        yield StreamEvent(delta=tail)

        raw_calls = [tool_acc[i] for i in sorted(tool_acc)]
        content = "".join(content_parts) if content_parts else None
        if content is not None:
            content = self._clean(content)
        yield StreamEvent(
            final=ChatResult(
                content=content,
                tool_calls=_parse_tool_calls(raw_calls),
            )
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> AsyncIterator[str]:
        async for event in self.stream_chat(messages, model=model):
            if event.delta:
                yield event.delta
