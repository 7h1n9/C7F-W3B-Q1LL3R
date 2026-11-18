"""The arena reproduction hook must never be able to downgrade a solve.

Every test here encodes the same rule from a different angle: the platform's
acceptance is authoritative, so no reproduction outcome -- timeout, exception,
missing run, missing target -- may change the verdict or raise out of the hook.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import arena_solve
from app.services.arena_solve import (
    REPRODUCTION_BUDGET_SECONDS,
    ArenaSolveOutcome,
    _verify_reproduction,
)


class FakeSession:
    """Minimal session double: only ``get`` is used by the hook.

    ``objects`` is consumed in call order rather than keyed by model class --
    the hook asks for ``SolveRun`` then ``Challenge``, and keying by class name
    would make the test depend on the hook's import style instead of its
    behaviour.
    """

    def __init__(self, objects=None):
        self._queue = list(objects or [])
        self.calls = 0

    async def get(self, model, key, **kwargs):
        self.calls += 1
        if not self._queue:
            return None
        return self._queue.pop(0)


class FakeLeaseManager:
    def __init__(self, raises: bool = False):
        self.raises = raises
        self.keep_alive_calls = 0

    async def keep_alive(self, client, lease):
        self.keep_alive_calls += 1
        if self.raises:
            raise RuntimeError("lease refresh failed")


def run(coro):
    return asyncio.run(coro)


def test_missing_run_id_is_skipped_not_an_error():
    result = run(
        _verify_reproduction(None, run_id="", lease_manager=None, lease=None, client=None)
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_run_id"
    assert result["verified"] is False


def test_missing_run_row_is_skipped():
    result = run(
        _verify_reproduction(
            FakeSession(), run_id="r1", lease_manager=None, lease=None, client=None
        )
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "run_not_found"


def test_lease_refresh_failure_does_not_abort_the_hook():
    manager = FakeLeaseManager(raises=True)
    result = run(
        _verify_reproduction(
            FakeSession(), run_id="r1", lease_manager=manager, lease=object(), client=None
        )
    )
    # It got past the lease and failed later (run lookup), proving the refresh
    # error was swallowed rather than propagated.
    assert manager.keep_alive_calls == 1
    assert result["status"] == "skipped"


def test_replay_exception_is_recorded_as_error():
    class ExplodingSession(FakeSession):
        async def get(self, model, key, **kwargs):
            raise RuntimeError("db is down")

    result = run(
        _verify_reproduction(
            ExplodingSession(), run_id="r1", lease_manager=None, lease=None, client=None
        )
    )
    assert result["status"] == "error"
    assert "RuntimeError" in result["reason"]
    assert result["verified"] is False


def test_replay_timeout_is_recorded_not_raised(monkeypatch):
    # A hung replay must not hold the platform's only environment slot.
    async def never(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(arena_solve, "REPRODUCTION_BUDGET_SECONDS", 0.05)

    class SlowSession(FakeSession):
        async def get(self, model, key, **kwargs):
            await never()
            return None

    result = run(
        _verify_reproduction(
            SlowSession(), run_id="r1", lease_manager=None, lease=None, client=None
        )
    )
    assert result["status"] == "timeout"
    assert result["verified"] is False


def test_successful_replay_reports_verified(monkeypatch):
    class FakeRun:
        challenge_id = "c1"

    class FakeChallenge:
        pass

    class FakePlanner:
        async def plan(self, session, run, challenge):
            return []

    class FakeExecutor:
        async def execute(self, session, run, challenge, steps):
            return {"executed": True, "verified": True, "blocked": ""}

    monkeypatch.setattr(
        "app.services.reports.ReproductionPlanner", lambda: FakePlanner()
    )
    monkeypatch.setattr(
        "app.services.fresh_reproduction.fresh_reproduction_executor", FakeExecutor()
    )

    session = FakeSession([FakeRun(), FakeChallenge()])
    result = run(
        _verify_reproduction(session, run_id="r1", lease_manager=None, lease=None, client=None)
    )
    assert result["status"] == "executed"
    assert result["verified"] is True


def test_replay_that_verifies_nothing_still_reports_executed(monkeypatch):
    class FakeRun:
        challenge_id = "c1"

    class FakePlanner:
        async def plan(self, session, run, challenge):
            return []

    class FakeExecutor:
        async def execute(self, session, run, challenge, steps):
            return {"executed": False, "verified": False, "blocked": "MISSING_PROVENANCE"}

    monkeypatch.setattr("app.services.reports.ReproductionPlanner", lambda: FakePlanner())
    monkeypatch.setattr(
        "app.services.fresh_reproduction.fresh_reproduction_executor", FakeExecutor()
    )
    session = FakeSession([FakeRun(), object()])
    result = run(
        _verify_reproduction(session, run_id="r1", lease_manager=None, lease=None, client=None)
    )
    assert result["status"] == "executed"
    assert result["verified"] is False
    assert result["blocked"] == "MISSING_PROVENANCE"


def test_budget_is_bounded():
    # Holding the platform's only slot demands a hard ceiling.
    assert 0 < REPRODUCTION_BUDGET_SECONDS <= 600


def test_outcome_defaults_to_no_reproduction():
    # A Run that never reached a submission must not look verified.
    assert ArenaSolveOutcome().fresh_reproduction == {}
