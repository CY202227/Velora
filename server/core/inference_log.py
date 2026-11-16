"""Recent inference events for the local operations dashboard."""

from collections import deque
from datetime import datetime, timezone
from math import isfinite

recent: deque[dict] = deque(maxlen=100)


def record(kind: str, model: str, seconds: float, tokens=None, *, status="ok", audio=None) -> None:
    count = tokens if type(tokens) is int and tokens >= 0 else None
    recent.appendleft({
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "model": model,
        "status": status,
        "audio": audio,
        "tokens": count,
        "seconds": seconds,
        "tokens_per_second": count / seconds
        if count is not None and seconds > 0 and isfinite(seconds) else None,
    })
