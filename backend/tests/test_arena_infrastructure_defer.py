"""An engine outage must not retire a challenge as unsolved.

Measured 2026-10-25: four consecutive arena runs (``88eacfa8``, ``75c89026``,
``1823ca8c``, ``a299a70b``) closed with ``NO_HEALTHY_WORKER_PROFILE`` and
**0 worker steps** each -- the Coordinator failed every PREPARE health check and
finished before creating one Intent.  Each burned ~250 seconds of the single
environment slot.

All four were filed ``UNSOLVED``.  That is wrong twice over:

* the item is retired, so it is never retried once the relay comes back; and
* the batch statistics read "four Easy Web boxes we could not solve" when
  nothing was ever attempted against them.

The queue already distinguishes busy / rate-limited / platform-unavailable for
exactly this reason.  A dead engine pool is the same class of event: it says
nothing about the challenge.
"""

from __future__ import annotations

from app.services.arena_queue import (
    INFRASTRUCTURE_NO_ATTEMPT_REASONS,
    _is_infrastructure_no_attempt,
)


def test_the_dead_engine_pool_is_recognised() -> None:
    """The exact reason string the Coordinator writes when every engine fails."""

    assert _is_infrastructure_no_attempt("NO_HEALTHY_WORKER_PROFILE")
    # Case and surrounding text must not matter: the reason travels through
    # several wrappers before the queue sees it.
    assert _is_infrastructure_no_attempt("no_healthy_worker_profile")
    assert _is_infrastructure_no_attempt("MUTEKI: NO_HEALTHY_WORKER_PROFILE (retry 2)")


def test_an_ordinary_unsolved_run_is_not_deferred() -> None:
    """A Run that genuinely worked the challenge must still retire the item.

    ``MUTEKI_RUN_TIMEOUT`` means the budget expired with the Worker still
    probing -- that IS a verdict on the box, and requeueing it would loop
    forever on a challenge that is simply harder than the budget.
    """

    for reason in (
        "MUTEKI_RUN_TIMEOUT",
        "FLAG_VERIFIED_AFTER_TIMEOUT",
        "",
        None,
        "MODEL_RATE_LIMITED",
    ):
        assert not _is_infrastructure_no_attempt(reason), reason


def test_a_worker_that_never_ran_is_deferred_too() -> None:
    """A CLI that reconnected in a loop says nothing about the challenge.

    Same class as the dead engine pool: the Run produced zero
    ``muteki.worker_command`` events and was closed by the no-progress bound, so
    the box was never actually attempted.
    """

    assert _is_infrastructure_no_attempt("WORKER_STALL_NO_COMMANDS")


def test_the_marker_list_documents_every_infrastructure_reason() -> None:
    """The tuple is the single place a new infrastructure reason is added."""

    assert "NO_HEALTHY_WORKER_PROFILE" in INFRASTRUCTURE_NO_ATTEMPT_REASONS
    assert "CREDENTIAL_RESOLUTION_FAILED" in INFRASTRUCTURE_NO_ATTEMPT_REASONS
    assert "WORKER_STALL_NO_COMMANDS" in INFRASTRUCTURE_NO_ATTEMPT_REASONS


def test_the_outcome_carries_the_reason() -> None:
    """The queue can only classify what the solve reports.

    ``ArenaSolveOutcome`` already carried ``run_status`` but not *why*, so the
    queue had nothing to test even if it wanted to.  Without this field the
    classification above is unreachable in production.
    """

    from app.services.arena_solve import ArenaSolveOutcome

    outcome = ArenaSolveOutcome()
    assert hasattr(outcome, "run_status_reason")
    # Default must be "no reason", which the classifier treats as an ordinary
    # verdict -- not as infrastructure trouble.
    assert outcome.run_status_reason == ""
    assert not _is_infrastructure_no_attempt(outcome.run_status_reason)
