"""Only retry transient model transport failures; never replay tools here."""

import httpx


def is_context_overflow(error: Exception) -> bool:
    if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code not in (400, 413, 422):
        return False
    try:
        body = error.response.json()
    except ValueError:
        return False
    detail = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        return False
    return detail.get("code") in {"context_length_exceeded", "context_window_exceeded", "max_context_length"} or detail.get("type") == "context_length_exceeded"


def is_transient(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
