"""The queue's "no engine was usable" guard needs a reason it can read.

``arena_queue._is_infrastructure_no_attempt`` exists so an upstream outage does
not retire a queue item: a Run that closed because no Worker engine passed its
health check never attempted the challenge, so filing it UNSOLVED would burn the
item permanently.

That guard was inert.  Measured 2026-11-05: of 75 Runs that closed on
``NO_HEALTHY_WORKER_PROFILE``, 74 had ``last_error_code IS NULL`` and the 75th
said ``MUTEKI_NATIVE_FINALIZED_RECONCILED_UNSOLVED``.  The Supervisor never
copies the Coordinator's stop reason onto the Run row -- it only exists in a
``muteki.dead_end`` event payload -- so ``outcome.run_status_reason`` was always
``""``, the guard returned False, and all 75 were filed as unsolved challenges.

``_dead_end_reason`` reads that payload.  These tests pin the parsing and the
ordering rule that makes it correct.
"""

from __future__ import annotations

import json

import pytest

from app.services.arena_queue import _is_infrastructure_no_attempt
from app.services.arena_solve import _dead_end_reason


class ScalarSession:
    """Returns one seeded payload for whatever query the helper builds."""

    def __init__(self, payload):
        self.payload = payload
        self.queries = 0

    async def scalar(self, _stmt):
        self.queries += 1
        return self.payload


async def reason_for(payload) -> str:
    return await _dead_end_reason(ScalarSession(payload), "run-1")


@pytest.mark.anyio
async def test_nested_coordinator_payload_is_unwrapped():
    payload = json.dumps(
        {"actor": "coordinator", "payload": {"payload": {"reason": "NO_HEALTHY_WORKER_PROFILE"}}}
    )
    assert await reason_for(payload) == "NO_HEALTHY_WORKER_PROFILE"


@pytest.mark.anyio
async def test_shallow_payload_is_also_accepted():
    payload = json.dumps({"payload": {"reason": "RACE_WORKER_TIMEOUT"}})
    assert await reason_for(payload) == "RACE_WORKER_TIMEOUT"


@pytest.mark.anyio
async def test_description_is_the_fallback_key():
    # Some producers write ``description`` rather than ``reason``.
    payload = json.dumps({"payload": {"description": "WORKER_TIMEOUT"}})
    assert await reason_for(payload) == "WORKER_TIMEOUT"


@pytest.mark.anyio
async def test_missing_payload_is_empty_not_an_exception():
    assert await reason_for(None) == ""


@pytest.mark.anyio
async def test_unparseable_payload_is_empty_not_an_exception():
    assert await reason_for("{not json") == ""


@pytest.mark.anyio
async def test_payload_without_a_reason_is_empty():
    assert await reason_for(json.dumps({"payload": {"ok": True}})) == ""


@pytest.mark.anyio
async def test_engine_outage_is_classified_as_no_attempt():
    """The whole point: this reason must reach the queue's guard."""

    payload = json.dumps({"payload": {"payload": {"reason": "NO_HEALTHY_WORKER_PROFILE"}}})
    reason = await reason_for(payload)
    assert _is_infrastructure_no_attempt(reason) is True


@pytest.mark.anyio
async def test_a_real_attempt_is_not_classified_as_no_attempt():
    """A Run that fought the box and lost must still spend its attempt."""

    payload = json.dumps({"payload": {"payload": {"reason": "RACE_WORKER_TIMEOUT"}}})
    reason = await reason_for(payload)
    assert _is_infrastructure_no_attempt(reason) is False
