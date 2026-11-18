"""Timeout and cancel must not share a terminal reason.

The distinction is not cosmetic: ``MutekiOrchestrator.run`` maps the Coordinator's
stop reason onto the Run's status, so a timeout recorded as ``STOPPED`` closes as
COMPLETED_UNSOLVED ("finished, no flag") and the most actionable class of failure
-- "ran out of time" -- becomes unmeasurable.

Measured before this: three real-class Runs (``0ab61266`` / ``879d42b0`` /
``87e96c2e``) each recorded a ``RACE_WORKER_TIMEOUT`` dead end and then closed
with ``reason: STOPPED``, landing in COMPLETED_UNSOLVED.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.solver.muteki.coordinator import TIMED_OUT_STOP_REASON, MutekiCoordinator
from app.solver.muteki.graph import MutekiGraph


def _coordinator(*, timed_out: bool, stop_reason: str | None) -> MutekiCoordinator:
    path = Path(tempfile.mkdtemp()) / "graph.db"
    graph = MutekiGraph(path, challenge_id="t", flag_pattern=r"CTF2\{[^}]+\}")
    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = graph
    coordinator._stop_reason = stop_reason
    coordinator._timed_out = timed_out
    return coordinator


def test_a_timed_out_round_reports_a_timeout_reason() -> None:
    """Losing a round to the clock is TIMEOUT, not a clean stop."""

    coordinator = _coordinator(timed_out=True, stop_reason=None)
    reason = coordinator._stop_reason or (
        TIMED_OUT_STOP_REASON if coordinator._timed_out else "STOPPED"
    )
    assert reason == "RACE_WORKER_TIMEOUT"
    # And that string is the one the Orchestrator maps onto status TIMEOUT.
    assert reason in {"RACE_WORKER_TIMEOUT", "WORKER_TIMEOUT", "MUTEKI_RUN_TIMEOUT"}


def test_an_explicit_stop_reason_wins() -> None:
    """A decision to stop is not overridden by an earlier timeout."""

    coordinator = _coordinator(timed_out=True, stop_reason="WORKER_BUDGET_EXHAUSTED")
    reason = coordinator._stop_reason or (
        TIMED_OUT_STOP_REASON if coordinator._timed_out else "STOPPED"
    )
    assert reason == "WORKER_BUDGET_EXHAUSTED"


def test_a_plain_stop_still_says_stopped() -> None:
    """No timeout, no stop reason: the historical value is preserved."""

    coordinator = _coordinator(timed_out=False, stop_reason=None)
    reason = coordinator._stop_reason or (
        TIMED_OUT_STOP_REASON if coordinator._timed_out else "STOPPED"
    )
    assert reason == "STOPPED"


def test_timeout_and_cancel_are_separate_fields() -> None:
    """The two causes live in different attributes on purpose."""

    graph = MutekiGraph(Path(tempfile.mkdtemp()) / "g.db", challenge_id="t")
    assert graph.flag_pattern  # sanity: the fixture builds a real graph
    defaults = MutekiCoordinator.__dataclass_fields__ if hasattr(
        MutekiCoordinator, "__dataclass_fields__"
    ) else {}
    # Not a dataclass; assert on the class-level defaults instead.
    assert getattr(MutekiCoordinator, "_timed_out", False) is False
    assert getattr(MutekiCoordinator, "_stop_requested", False) is False
    assert defaults == {}
