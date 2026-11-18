"""One shared reading of "MySQL made us wait too long for a lock".

One place hits this and failed silently:

* :mod:`app.services.run_attempts` inserts ``run_execution_leases`` at Run
  start.  Losing meant the Run died as ``FAILED_ENGINE`` within a minute —
  measured on runs ``9f221910``, ``0dcc1934`` and ``b76030e8``, all during the
  burst of transactions right after a backend restart.

A lock wait is transient by definition: the holder commits in milliseconds.
Retrying is therefore correct, while treating it as a real error is not.
"""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# 1205 = lock wait timeout, 1213 = deadlock victim.  Both mean "try again".
TRANSIENT_LOCK_CODES = frozenset({1205, 1213})


def is_transient_lock_error(error: BaseException) -> bool:
    """True when ``error`` is a lock wait MySQL wants us to retry."""

    args = getattr(getattr(error, "orig", None), "args", None)
    if not args:
        return False
    try:
        return int(args[0]) in TRANSIENT_LOCK_CODES
    except (TypeError, ValueError):
        return False


def lock_retry_delay(attempt: int) -> float:
    """Bounded jittered backoff for retry ``attempt`` (0-based)."""

    return 0.05 * (2**attempt) + random.random() * 0.05


async def sleep_before_lock_retry(attempt: int) -> None:
    await asyncio.sleep(lock_retry_delay(attempt))


__all__ = [
    "TRANSIENT_LOCK_CODES",
    "is_transient_lock_error",
    "lock_retry_delay",
    "sleep_before_lock_retry",
]
