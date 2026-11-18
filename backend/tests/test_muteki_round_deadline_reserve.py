"""A Worker round that cannot finish must not be started.

Run ``49af30bf`` (20-minute budget, 720-second Worker round budget) ended with
three directions closed ``timed_out`` and ``result_detail=null``.  The rounds
were not idle -- they were *reaped*: the deadline landed while they were still
verifying, so Finalize swept them and their results were lost.

The convergence window cannot prevent that.  It is capped well below the Worker
round budget (it must stay under the Runtime's Finalize grace), so it only ever
captures a round that is nearly done; the round in that Run had ~270s left when
the 120s window opened.  What prevents it is declining to *start* a round that
cannot finish: the deadline then arrives with the pool already drained, every
direction is closed by its own real result, and the Run leaves through the
normal path.

The tests pin both halves of that: the decision itself, and the fact that
declining is paired with convergence (otherwise the loop would idle until the
no-progress guard tripped instead of draining).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.solver.muteki.coordinator import CoordinatorConfig, MutekiCoordinator

ROUND_BUDGET = 30.0
GRACE = 5.0


def _coordinator(*, seconds_left: float | None) -> MutekiCoordinator:
    """A Coordinator with only the fields the dispatch guard reads.

    Mirrors the ``object.__new__`` style the focused Coordinator tests use: the
    guard must answer before ``__init__`` has run, because Finalize reads it on
    every terminal path.
    """

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.config = CoordinatorConfig(
        worker_timeout_seconds=int(ROUND_BUDGET),
        worker_wait_grace_seconds=GRACE,
        run_deadline_monotonic=(
            None if seconds_left is None else time.monotonic() + seconds_left
        ),
    )
    coordinator.stage_policy = SimpleNamespace(
        can_spawn=lambda *_args, **_kwargs: True,
        get_max_workers=lambda _phase: 10,
    )
    coordinator.pool = SimpleNamespace(active_count=0, max_workers=10)
    coordinator.phase = SimpleNamespace(value="coordinator")
    return coordinator


# ── the decision ─────────────────────────────────────────────────────────


def test_a_round_fits_when_there_is_no_deadline() -> None:
    """No deadline means the caller owns the bound; dispatch stays unconditional."""

    assert _coordinator(seconds_left=None)._a_round_still_fits() is True


def test_a_round_fits_when_the_whole_budget_is_left() -> None:
    assert _coordinator(seconds_left=600.0)._a_round_still_fits() is True


def test_a_round_does_not_fit_when_less_than_its_budget_remains() -> None:
    """This is the Run that lost three directions to the reaper."""

    assert _coordinator(seconds_left=ROUND_BUDGET - 1.0)._a_round_still_fits() is False


def test_the_grace_counts_towards_the_round() -> None:
    """A round's real worst case is its timeout plus the teardown grace."""

    # Exactly the timeout is not enough: the grace still has to fit.
    assert _coordinator(seconds_left=ROUND_BUDGET)._a_round_still_fits() is False
    assert _coordinator(seconds_left=ROUND_BUDGET + GRACE)._a_round_still_fits() is True


# ── the dispatch point ───────────────────────────────────────────────────


def test_spawn_is_refused_when_the_round_cannot_finish() -> None:
    coordinator = _coordinator(seconds_left=1.0)
    coordinator._worker_number = 0  # type: ignore[attr-defined]

    spawned = asyncio.run(
        coordinator._spawn(SimpleNamespace(engine_id="claude-cli:x"), role="race")
    )

    assert spawned is False
    # Declining must also mean "stop opening directions", or the loop would spin.
    assert coordinator._converging is True


def test_the_refusal_reason_is_recorded() -> None:
    coordinator = _coordinator(seconds_left=1.0)

    asyncio.run(coordinator._spawn(SimpleNamespace(engine_id="claude-cli:x"), role="race"))

    assert coordinator._converge_reason == "RUN_BUDGET_RESERVED_FOR_LAST_ROUND"


def test_reserving_the_last_round_does_not_downgrade_a_stop() -> None:
    """A Run already stopping must not be walked back into convergence."""

    coordinator = _coordinator(seconds_left=1.0)
    coordinator._stop_requested = True

    coordinator._reserve_the_last_round()

    assert coordinator._converging is False


def test_reserving_the_last_round_is_idempotent() -> None:
    coordinator = _coordinator(seconds_left=1.0)

    coordinator._reserve_the_last_round()
    first = coordinator._converge_deadline
    coordinator._reserve_the_last_round()

    assert coordinator._converge_deadline == first, "the window must not be extended"


# ── the runtime hands the deadline over ──────────────────────────────────


def test_the_orchestrator_passes_the_deadline_to_the_coordinator(tmp_path) -> None:
    """Without this the guard would answer "no deadline" in production."""

    from app.solver.muteki.core.orchestrator import MutekiOrchestrator
    from app.solver.muteki.graph import MutekiGraph
    from app.solver.muteki.reason import MutekiReason
    from app.solver.muteki.workers import EngineProfile

    graph = MutekiGraph(tmp_path / "deadline.sqlite", challenge_id="challenge-deadline")

    async def runner(_job):
        return None

    deadline = time.monotonic() + 1200.0
    orchestrator = MutekiOrchestrator(
        graph,
        MutekiReason(),
        worker_runner=runner,
        engines=[EngineProfile("gateway-runner")],
        interval_seconds=0.0,
        run_deadline_monotonic=deadline,
    )

    assert orchestrator.coordinator.config.run_deadline_monotonic == deadline


def test_the_orchestrator_omits_the_deadline_when_the_caller_has_none(tmp_path) -> None:
    from app.solver.muteki.core.orchestrator import MutekiOrchestrator
    from app.solver.muteki.graph import MutekiGraph
    from app.solver.muteki.reason import MutekiReason
    from app.solver.muteki.workers import EngineProfile

    graph = MutekiGraph(tmp_path / "no-deadline.sqlite", challenge_id="challenge-no-deadline")

    async def runner(_job):
        return None

    orchestrator = MutekiOrchestrator(
        graph,
        MutekiReason(),
        worker_runner=runner,
        engines=[EngineProfile("gateway-runner")],
        interval_seconds=0.0,
    )

    assert orchestrator.coordinator.config.run_deadline_monotonic is None
