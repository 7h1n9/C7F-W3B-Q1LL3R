"""Run-deadline coverage for the Muteki Runtime boundary.

The Run-level total runtime is a *cooperative* stop: ``Coordinator.run`` owns
its terminal ``Finalize`` (and the ``RUN_FINISHED`` handoff), so Runtime must
await that normal return path instead of cancelling it.  These tests keep that
path real — a genuine ``MutekiOrchestrator``/``MutekiCoordinator``/Worker-pool
lifecycle over a real ``MutekiGraph`` with a Worker that deliberately outlives
the deadline — and cover the two ways the wait itself used to be wrong: a bare
``await run_task`` that let an outer cancellation cancel the Orchestrator Task
while Finalize was already in flight, and an unbounded wait that could pin a
wedged Finalize open forever.
"""

from __future__ import annotations

import asyncio

from app.solver.muteki import (
    EngineProfile,
    EventType,
    MutekiGraph,
    MutekiReason,
)
from app.solver.muteki.core.orchestrator import MutekiOrchestrator
from app.solver.muteki.runtime import muteki_runtime
from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime


def _runtime_boundary(graph: MutekiGraph) -> MutekiRuntime:
    """Build only the Runtime attributes the deadline boundary needs."""

    runtime = object.__new__(MutekiRuntime)
    runtime._run_id = graph.challenge_id
    runtime._graph = graph
    return runtime


def _orchestrator(
    graph: MutekiGraph,
    runner,
    *,
    worker_timeout_seconds: int = 1,
) -> MutekiOrchestrator:
    return MutekiOrchestrator(
        graph,
        MutekiReason(),
        worker_runner=runner,
        engines=[EngineProfile("gateway-runner")],
        interval_seconds=0.0,
        worker_timeout_seconds=worker_timeout_seconds,
    )


def _phases(graph: MutekiGraph) -> list[str]:
    return [
        str(item.payload.get("phase"))
        for item in graph.events_since()
        if item.event_type == EventType.PHASE_CHANGED
    ]


def _terminal_events(graph: MutekiGraph) -> list:
    return [
        item for item in graph.events_since() if item.event_type == EventType.RUN_FINISHED
    ]


def _event_index(graph: MutekiGraph, event_type: EventType, **payload) -> int:
    for index, event in enumerate(graph.events_since()):
        if event.event_type != event_type:
            continue
        if all(event.payload.get(key) == value for key, value in payload.items()):
            return index
    raise AssertionError(f"{event_type} with payload {payload!r} not found")


def _dead_ends(graph: MutekiGraph) -> list[str]:
    return [str(item.description) for item in graph.dead_ends()]


async def test_run_deadline_reaches_coordinator_finalize(tmp_path) -> None:
    """A Run deadline must still close the loop through the terminal handoff."""

    graph = MutekiGraph(tmp_path / "run-deadline.sqlite", challenge_id="challenge-run-deadline")
    worker_started = asyncio.Event()

    async def runner(_job):
        # Deliberately outlives the whole Run budget: Finalize owns the Worker
        # reap, and the deadline must not drop the terminal handoff instead.
        worker_started.set()
        await asyncio.Event().wait()

    orchestrator = _orchestrator(graph, runner)
    runtime = _runtime_boundary(graph)

    result = await asyncio.wait_for(
        runtime._run_orchestrator_with_deadline(
            orchestrator,
            max_rounds=None,
            total_timeout=0.01,
        ),
        timeout=20,
    )

    assert worker_started.is_set()
    assert result.status == "TIMEOUT"
    assert result.reason == "MUTEKI_RUN_TIMEOUT"
    assert result.flag_found is False
    assert _phases(graph) == ["prepare", "race", "finalize"]
    finalize_index = _event_index(
        graph,
        EventType.PHASE_CHANGED,
        phase="finalize",
    )
    intent_closure_index = _event_index(
        graph,
        EventType.COORDINATOR_DIRECTIVE,
        directive="finalize_active_intents",
    )
    finished_index = _event_index(
        graph,
        EventType.RUN_FINISHED,
        reason="MUTEKI_RUN_TIMEOUT",
    )
    assert finalize_index < intent_closure_index < finished_index
    terminal = _terminal_events(graph)
    assert len(terminal) == 1
    assert terminal[0].payload["reason"] == "MUTEKI_RUN_TIMEOUT"
    assert terminal[0].payload["flag_found"] is False
    assert "MUTEKI_RUN_TIMEOUT" in _dead_ends(graph)


async def test_outer_cancellation_during_finalize_keeps_terminal_handoff(tmp_path) -> None:
    """A Run stop during Finalize must not cancel the terminal handoff."""

    graph = MutekiGraph(
        tmp_path / "cancel-during-finalize.sqlite",
        challenge_id="challenge-cancel-during-finalize",
    )
    finalize_entered = asyncio.Event()
    release_finalize = asyncio.Event()

    async def runner(_job):
        await asyncio.Event().wait()

    orchestrator = _orchestrator(graph, runner)
    # Finalize's Worker reclaim is its first long await.  Parking there makes
    # the outer cancellation land *inside* Finalize instead of before it.
    pool = orchestrator.coordinator.pool
    reclaim_workers = pool.cancel_all

    async def parked_cancel_all() -> None:
        finalize_entered.set()
        await release_finalize.wait()
        await reclaim_workers()

    pool.cancel_all = parked_cancel_all
    runtime = _runtime_boundary(graph)
    run_task = asyncio.create_task(
        runtime._run_orchestrator_with_deadline(
            orchestrator,
            max_rounds=None,
            total_timeout=0.05,
        )
    )

    await asyncio.sleep(0.1)  # the Run deadline has passed
    await asyncio.wait_for(finalize_entered.wait(), timeout=5)
    run_task.cancel()  # outer Run stop / service shutdown
    await asyncio.sleep(0.05)  # the Runtime consumes the cancellation
    release_finalize.set()
    outcome = await asyncio.wait_for(
        asyncio.gather(run_task, return_exceptions=True),
        timeout=20,
    )

    assert isinstance(outcome[0], asyncio.CancelledError)
    terminal = _terminal_events(graph)
    assert len(terminal) == 1
    assert terminal[0].payload["reason"] == "MUTEKI_RUN_TIMEOUT"
    assert _phases(graph)[-1] == "finalize"


async def test_wedged_finalize_is_explicit_and_bounded(tmp_path, monkeypatch) -> None:
    """A Finalize that outlives its grace must be recorded, not hidden."""

    monkeypatch.setattr(muteki_runtime, "FINALIZE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(muteki_runtime, "FINALIZE_RECLAIM_SECONDS", 0.05)
    graph = MutekiGraph(tmp_path / "wedged-finalize.sqlite", challenge_id="challenge-wedged-finalize")
    finalize_entered = asyncio.Event()
    release_wedged = asyncio.Event()

    async def runner(_job):
        await asyncio.Event().wait()

    orchestrator = _orchestrator(graph, runner)
    pool = orchestrator.coordinator.pool

    async def wedged_cancel_all() -> None:
        finalize_entered.set()
        await release_wedged.wait()  # outlives the whole grace window

    pool.cancel_all = wedged_cancel_all
    runtime = _runtime_boundary(graph)

    result = await asyncio.wait_for(
        runtime._run_orchestrator_with_deadline(
            orchestrator,
            max_rounds=None,
            total_timeout=0.01,
        ),
        timeout=20,
    )

    assert finalize_entered.is_set()
    assert result.status == "TIMEOUT"
    assert result.reason == "MUTEKI_RUN_TIMEOUT"
    assert result.flag_found is False
    assert "MUTEKI_FINALIZE_GRACE_EXCEEDED" in _dead_ends(graph)
    # Finalize never completed, so no terminal handoff may be claimed.
    assert _terminal_events(graph) == []
    # Let the abandoned Coordinator unwind before the loop is torn down; the
    # Run has already been reported at this point.
    release_wedged.set()
    await asyncio.sleep(0)


# ── the worker round cap (2026-10-25) ──────────────────────────────────────


def test_the_worker_round_cap_comes_from_the_arena_budget() -> None:
    """A flat 720 was the real ceiling on every measured arena solve.

    Nine consecutive Easy Web runs ended at 897-1085s against a 1200s Run
    budget, so none of them ran out of *total* time.  Their worker phase ended
    at 755-760s every single time -- ``min(720, max_runtime_seconds)`` plus
    launch overhead.  The Runs were not converging, they were being reaped at
    twelve minutes after 150-320 reconnaissance steps, and then spending what
    was left on a single Intent.

    The cap therefore has to track the arena's own number instead of being
    frozen in the runtime, or raising the Run budget changes nothing.
    """

    from app.services.arena_solve import ARENA_WORKER_ROUND_SECONDS
    from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime

    runtime = MutekiRuntime.__new__(MutekiRuntime)
    assert runtime._worker_round_cap() == ARENA_WORKER_ROUND_SECONDS
    # The whole point: strictly above the old hard-coded ceiling.
    assert runtime._worker_round_cap() > 720


def test_the_cap_cannot_outlive_the_run_budget() -> None:
    """A Worker round must not exceed the Run that owns it.

    The deadline reaps anything still running when the Run budget expires, and
    every direction that Worker was verifying closes as ``timed_out`` with no
    result -- so a cap above the budget does not buy time, it guarantees the
    round is killed with nothing to show for it.
    """

    from app.services.arena_solve import (
        ARENA_RUN_BUDGET_SECONDS,
        ARENA_WORKER_ROUND_SECONDS,
        RUN_TIMEOUT_SECONDS,
    )
    from app.solver.muteki.runtime.muteki_runtime import RUN_CONVERGENCE_SECONDS

    assert ARENA_WORKER_ROUND_SECONDS <= ARENA_RUN_BUDGET_SECONDS
    # Convergence and finalisation still have to fit after the round.
    assert ARENA_WORKER_ROUND_SECONDS + RUN_CONVERGENCE_SECONDS <= ARENA_RUN_BUDGET_SECONDS
    # The outer wait must outlast the Run, or the environment is destroyed
    # underneath a solve that is still legitimately working.
    assert RUN_TIMEOUT_SECONDS > ARENA_RUN_BUDGET_SECONDS


def test_convergence_is_still_the_safety_valve() -> None:
    """Raising the worker cap must not raise the convergence window.

    ``RUN_CONVERGENCE_SECONDS`` bounds how long a drifting Run may spend
    converging, and it is a safety valve rather than a difficulty knob.  Time
    spent investigating belongs in the worker round; time spent drifting does
    not get more room just because the round got more.
    """

    from app.solver.muteki.runtime.muteki_runtime import RUN_CONVERGENCE_SECONDS

    assert RUN_CONVERGENCE_SECONDS == 120.0
