"""Run start must survive a lock wait instead of dying as FAILED_ENGINE.

Measured on 2026-10-20: right after a backend restart, runs ``9f221910``,
``0dcc1934`` and ``b76030e8`` all failed within a minute with
``1205 Lock wait timeout exceeded`` on ``INSERT INTO run_execution_leases``.
Each one was a challenge that never got to run at all — worse than a failed
solve, because nothing in the result distinguishes it from a solver defect.

Falsifiable: setting ``_BEGIN_LOCK_RETRIES`` to 1 makes the retry test fail.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.core.db_retry import is_transient_lock_error  # noqa: E402
from app.core.exceptions import DomainError  # noqa: E402
from app.services import run_attempts as module  # noqa: E402
from app.services.run_attempts import RunAttemptService  # noqa: E402


def _lock_wait_error() -> OperationalError:
    return OperationalError("INSERT", {}, Exception(1205, "Lock wait timeout exceeded"))


def _deadlock_error() -> OperationalError:
    return OperationalError("INSERT", {}, Exception(1213, "Deadlock found when trying to get lock"))


class _FakeSession:
    """Stands in for an ``AsyncSession`` whose identity map holds caller state."""

    def __init__(self, run: SimpleNamespace, loaded: list[SimpleNamespace] | None = None) -> None:
        self.run = run
        self.loaded = loaded or []
        self.rollbacks = 0
        self.gets = 0
        self.refreshed: list[SimpleNamespace] = []
        self.sync_session = SimpleNamespace(identity_map={})

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(self, _model, _run_id):
        self.gets += 1
        return self.run

    async def refresh(self, instance) -> None:
        self.refreshed.append(instance)


def _run() -> SimpleNamespace:
    return SimpleNamespace(id="run-lock-1", status="RUNNING", thread_invalidated=False)


def test_lock_wait_errors_are_classified_transient() -> None:
    assert is_transient_lock_error(_lock_wait_error()) is True
    assert is_transient_lock_error(_deadlock_error()) is True
    # A genuine failure must not be retried four times.
    assert is_transient_lock_error(OperationalError("INSERT", {}, Exception(1062, "Duplicate entry"))) is False
    assert is_transient_lock_error(RuntimeError("boom")) is False


def test_begin_retries_a_lock_wait_and_wins(monkeypatch) -> None:
    run = _run()
    session = _FakeSession(run)
    calls: list[int] = []

    async def fake_begin_once(_self, _session, _run):
        calls.append(len(calls))
        if len(calls) == 1:
            raise _lock_wait_error()
        return ("attempt", "lease")

    monkeypatch.setattr(RunAttemptService, "_begin_once", fake_begin_once)
    monkeypatch.setattr(module, "sleep_before_lock_retry", lambda _attempt: asyncio.sleep(0))

    attempt, lease = asyncio.run(RunAttemptService().begin(session, run))  # type: ignore[arg-type]

    assert (attempt, lease) == ("attempt", "lease")
    assert len(calls) == 2, "one lock wait must not end the Run"
    # The rolled-back transaction expired the ORM instance, so Run is re-read.
    assert session.rollbacks == 1
    assert session.gets == 1


def test_a_retry_reloads_instances_the_caller_already_holds(monkeypatch) -> None:
    """``rollback()`` expires everything, which is fatal under asyncio.

    Measured on run ``2d8105f2``: ``_run_muteki`` loads the Challenge before
    ``begin``, so the retry's rollback left it expired and the very next
    attribute read raised ``greenlet_spawn has not been called`` — the Run died
    of the recovery, not of the lock wait.
    """

    run = _run()
    challenge = SimpleNamespace(id="challenge-1")
    session = _FakeSession(run)
    session.sync_session.identity_map = {"challenge-1": challenge}

    calls: list[int] = []

    async def fake_begin_once(_self, _session, _run):
        calls.append(len(calls))
        if len(calls) == 1:
            raise _lock_wait_error()
        return ("attempt", "lease")

    monkeypatch.setattr(RunAttemptService, "_begin_once", fake_begin_once)
    monkeypatch.setattr(module, "sleep_before_lock_retry", lambda _attempt: asyncio.sleep(0))

    asyncio.run(RunAttemptService().begin(session, run))  # type: ignore[arg-type]

    # Falsifiable: without the reload the caller's Challenge stays expired.
    assert challenge in session.refreshed, session.refreshed


def test_a_real_conflict_is_not_retried(monkeypatch) -> None:
    run = _run()
    session = _FakeSession(run)
    calls: list[int] = []

    async def fake_begin_once(_self, _session, _run):
        calls.append(len(calls))
        raise DomainError("RUN_ALREADY_EXECUTING", "Run already has an active execution lease.", status_code=409)

    monkeypatch.setattr(RunAttemptService, "_begin_once", fake_begin_once)
    monkeypatch.setattr(module, "sleep_before_lock_retry", lambda _attempt: asyncio.sleep(0))

    with pytest.raises(DomainError) as raised:
        asyncio.run(RunAttemptService().begin(session, run))  # type: ignore[arg-type]

    assert raised.value.code == "RUN_ALREADY_EXECUTING"
    # Falsifiable: a blanket retry would hide the 409 behind three more attempts.
    assert len(calls) == 1, calls


def test_the_retry_budget_is_bounded(monkeypatch) -> None:
    run = _run()
    session = _FakeSession(run)
    calls: list[int] = []

    async def fake_begin_once(_self, _session, _run):
        calls.append(len(calls))
        raise _lock_wait_error()

    monkeypatch.setattr(RunAttemptService, "_begin_once", fake_begin_once)
    monkeypatch.setattr(module, "sleep_before_lock_retry", lambda _attempt: asyncio.sleep(0))

    with pytest.raises(OperationalError):
        asyncio.run(RunAttemptService().begin(session, run))  # type: ignore[arg-type]

    assert len(calls) == module._BEGIN_LOCK_RETRIES, calls
    assert session.rollbacks == module._BEGIN_LOCK_RETRIES - 1
