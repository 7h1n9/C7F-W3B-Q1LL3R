"""Health-probe retry must absorb a blip without masking a real failure.

Measured 2026-11-04 00:43-01:00: both lanes alternated
``WORKER_HEALTHCHECK_TIMEOUT`` / ``WORKER_HEALTHCHECK_FAILED`` for twenty
minutes against an overloaded gateway, and every challenge in that window was
burned because a single failed probe retires the engine for the whole Run.
"""

from __future__ import annotations

import asyncio

import pytest

from app.solver.muteki.worker import official_worker
from app.solver.muteki.worker.official_worker import (
    _HEALTH_RETRY_DELAY_SECONDS,
    _TRANSIENT_HEALTH_REASONS,
    _health_failure_is_transient,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_timeout_is_transient():
    assert _health_failure_is_transient("WORKER_HEALTHCHECK_TIMEOUT")


def test_generic_probe_failure_is_transient():
    assert _health_failure_is_transient("WORKER_HEALTHCHECK_FAILED")


def test_empty_reason_is_not_transient():
    # A probe that returned False with no reason is a logic problem, not a blip.
    assert not _health_failure_is_transient("")


def test_unknown_reason_is_not_transient():
    assert not _health_failure_is_transient("SOMETHING_ELSE_ENTIRELY")


def test_transient_set_stays_narrow():
    # A broad set would retry genuine misconfiguration and double the cost.
    assert _TRANSIENT_HEALTH_REASONS == {
        "WORKER_HEALTHCHECK_TIMEOUT",
        "WORKER_HEALTHCHECK_FAILED",
    }


def test_retry_delay_is_short():
    assert 0 < _HEALTH_RETRY_DELAY_SECONDS <= 30


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


class StubWorker:
    """Stands in for the worker, replaying a scripted sequence of probes."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def _health_once(self, profile, *, workspace):
        self.calls += 1
        return self._results.pop(0)


def call_health(worker):
    return run(official_worker.OfficialWorkerAdapter.health(worker, object(), workspace="/tmp"))


def test_first_probe_success_does_not_retry():
    worker = StubWorker([(True, "")])
    assert call_health(worker) == (True, "")
    assert worker.calls == 1


def test_transient_failure_is_retried_once(monkeypatch):
    monkeypatch.setattr(official_worker, "_HEALTH_RETRY_DELAY_SECONDS", 0)
    worker = StubWorker([(False, "WORKER_HEALTHCHECK_TIMEOUT"), (True, "")])
    assert call_health(worker) == (True, "")
    assert worker.calls == 2


def test_retry_success_returns_empty_reason(monkeypatch):
    # A recovered engine must not keep carrying the failure code, or the
    # coordinator still classifies the Run as never-attempted.
    monkeypatch.setattr(official_worker, "_HEALTH_RETRY_DELAY_SECONDS", 0)
    worker = StubWorker([(False, "WORKER_HEALTHCHECK_FAILED"), (True, "")])
    healthy, reason = call_health(worker)
    assert healthy is True and reason == ""


def test_persistent_failure_reports_the_second_reason(monkeypatch):
    monkeypatch.setattr(official_worker, "_HEALTH_RETRY_DELAY_SECONDS", 0)
    worker = StubWorker(
        [
            (False, "WORKER_HEALTHCHECK_TIMEOUT"),
            (False, "WORKER_HEALTHCHECK_TIMEOUT"),
        ]
    )
    healthy, reason = call_health(worker)
    assert healthy is False
    assert reason == "WORKER_HEALTHCHECK_TIMEOUT"
    assert worker.calls == 2


def test_retry_happens_at_most_once(monkeypatch):
    # Three consecutive failures must not become three probes: the Run budget
    # is the scarcer resource.
    monkeypatch.setattr(official_worker, "_HEALTH_RETRY_DELAY_SECONDS", 0)
    worker = StubWorker(
        [
            (False, "WORKER_HEALTHCHECK_TIMEOUT"),
            (False, "WORKER_HEALTHCHECK_TIMEOUT"),
            (True, ""),
        ]
    )
    assert call_health(worker)[0] is False
    assert worker.calls == 2


def test_non_transient_failure_is_not_retried():
    # A rejected credential fails identically twice; retrying only wastes budget.
    worker = StubWorker([(False, "WORKER_AUTH_REJECTED")])
    healthy, reason = call_health(worker)
    assert healthy is False
    assert reason == "WORKER_AUTH_REJECTED"
    assert worker.calls == 1
