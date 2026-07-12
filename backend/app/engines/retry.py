from __future__ import annotations

from collections.abc import Mapping

TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def backoff_delay(
    attempt: int,
    *,
    base: float = 0.75,
    cap: float = 12.0,
    retry_after: float | None = None,
) -> float:
    delay = base * (2**attempt)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(delay, cap)

