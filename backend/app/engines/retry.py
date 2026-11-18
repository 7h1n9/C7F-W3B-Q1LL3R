from __future__ import annotations

import random
from collections.abc import Callable, Mapping

TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}

# A purely deterministic delay (base * 2**attempt) makes every concurrent Run
# retry in lockstep, which is how a short provider outage becomes a self-
# inflicted thundering herd.  Multiplicative ("equal") jitter spreads the herd
# while keeping the mean wait unchanged.
DEFAULT_RETRY_JITTER = 0.5


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
    jitter: float = 0.0,
    rng: Callable[[], float] | None = None,
) -> float:
    """Return the wait before retry ``attempt`` (0-based).

    ``jitter`` is the relative spread applied multiplicatively, so the result
    lands in ``[delay * (1 - jitter), delay]`` scaled by ``cap``.  The default
    ``jitter=0.0`` preserves the legacy deterministic value exactly, which
    keeps every existing caller and its tests byte-for-byte identical.
    """

    delay = base * (2**attempt)
    if retry_after is not None:
        delay = max(delay, retry_after)
    delay = min(delay, cap)

    if jitter <= 0:
        return delay

    source = rng if rng is not None else random.random
    jittered = delay * (1.0 + jitter * (2.0 * source() - 1.0))
    # A provider-supplied Retry-After is a contract, not a hint: jitter may
    # spread the wait, but it must never undercut what the provider asked for.
    floor = min(retry_after, cap) if retry_after is not None else 0.0
    return min(max(jittered, floor), cap)

