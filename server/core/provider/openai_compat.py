"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from server.core.provider.base import ChatResult, ToolCall

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
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
        return ChatResult(content=content, tool_calls=tool_calls)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> AsyncIterator[str]:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
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
                    content = delta.get("content")
                    if content:
                        yield content
