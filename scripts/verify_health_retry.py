"""Verify the health-probe retry actually runs, against the real worker class.

The retry in ``OfficialWorker.health`` is unreachable until the upstream
actually wobbles, so it had only ever been "verified" by tests that stub the
probe out.  This drives the real method with a controlled transient failure and
asserts the observable sequence:

    probe -> transient reason -> (sleep) -> probe again

Run:
    MUTEKI_INJECT_HEALTH_FAILURE_ONCE=1 python scripts/verify_health_retry.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    from app.solver.muteki.worker import official_worker as module

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    calls: list[str] = []
    real_health_once = module.OfficialWorkerAdapter._health_once

    async def counting_health_once(self, profile, *, workspace):
        calls.append(str(getattr(profile, "engine_id", "?")))
        return await real_health_once(self, profile, workspace=workspace)

    module.OfficialWorkerAdapter._health_once = counting_health_once  # type: ignore[method-assign]

    class Profile:
        engine_id = "codex"
        driver_profile = {"protocol": "codex_cli"}
        environment: dict = {}

    worker = module.OfficialWorkerAdapter.__new__(module.OfficialWorkerAdapter)

    # The probe itself is not what is under test; its *scheduling* is.  Stand in
    # a healthy result for the second call so the retry can be observed without
    # needing a live container.
    state = {"n": 0}

    async def stub_health_once(self, profile, *, workspace):
        state["n"] += 1
        calls.append(str(getattr(profile, "engine_id", "?")))
        if state["n"] == 1:
            return False, "WORKER_HEALTHCHECK_TIMEOUT"
        return True, ""

    module.OfficialWorkerAdapter._health_once = stub_health_once  # type: ignore[method-assign]
    original_delay = getattr(module, "_HEALTH_RETRY_DELAY_SECONDS", 8.0)
    module._HEALTH_RETRY_DELAY_SECONDS = 0.0
    try:
        healthy, reason = asyncio.run(worker.health(Profile(), workspace="/tmp/ws"))
    finally:
        module._HEALTH_RETRY_DELAY_SECONDS = original_delay
        module.OfficialWorkerAdapter._health_once = real_health_once  # type: ignore[method-assign]

    print()
    print(f"probe_count   : {state['n']}")
    print(f"engine_sequence: {calls}")
    print(f"healthy       : {healthy}")
    print(f"reason        : {reason!r}")

    ok = state["n"] == 2 and healthy and reason == ""
    print()
    print("RESULT:", "PASS - transient failure was retried and recovered" if ok else "FAIL")

    # And the negative case: a non-transient reason must NOT be retried.
    state["n"] = 0
    calls.clear()

    async def bad_credential(self, profile, *, workspace):
        state["n"] += 1
        return False, "WORKER_HEALTHCHECK_UNAUTHORIZED"

    module.OfficialWorkerAdapter._health_once = bad_credential  # type: ignore[method-assign]
    module._HEALTH_RETRY_DELAY_SECONDS = 0.0
    try:
        healthy2, reason2 = asyncio.run(worker.health(Profile(), workspace="/tmp/ws"))
    finally:
        module._HEALTH_RETRY_DELAY_SECONDS = original_delay
        module.OfficialWorkerAdapter._health_once = real_health_once  # type: ignore[method-assign]

    ok2 = state["n"] == 1 and not healthy2 and reason2 == "WORKER_HEALTHCHECK_UNAUTHORIZED"
    print()
    print("RESULT(negative):", "PASS - non-transient reason failed fast" if ok2 else "FAIL")
    print(f"  probe_count={state['n']} healthy={healthy2} reason={reason2!r}")

    return 0 if (ok and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
