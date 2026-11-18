"""Stop-convergence coverage for the Muteki Coordinator/Runtime boundary.

A Run deadline used to reap whatever Worker round was in flight, so a direction
that was one tool call away from a verified fact ended up reading as "stopped
before its verification completed".  The window these tests exercise is the fix:
the last slice of the Run budget is handed to the round that is already running.

These are behavioural tests, not mock assertions: they drive the real
orchestrator/coordinator/worker-pool lifecycle over a real graph, and the
Worker's completion is exactly the thing the window exists to protect.  The
paired negative case (a hard stop still reaps the round) is what keeps the
window honest -- it must not turn every stop into a two-minute wait.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.solver.muteki import EngineProfile, EventType, MutekiGraph, MutekiReason
from app.solver.muteki.coordinator import CoordinatorConfig, MutekiCoordinator
from app.solver.muteki.core.orchestrator import MutekiOrchestrator
from app.solver.muteki.runtime import muteki_runtime
from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime
from app.solver.muteki.workers import WorkerOutcome

_PROTECTED_FACT = "round-verified-observation"


def _graph(tmp_path, name: str) -> MutekiGraph:
    return MutekiGraph(tmp_path / f"{name}.sqlite", challenge_id=name)


def _orchestrator(graph: MutekiGraph, runner, *, worker_timeout_seconds: int = 5):
    return MutekiOrchestrator(
        graph,
        MutekiReason(),
        worker_runner=runner,
        engines=[EngineProfile("gateway-runner")],
        interval_seconds=0.0,
        worker_timeout_seconds=worker_timeout_seconds,
    )


def _runtime_boundary(graph: MutekiGraph) -> MutekiRuntime:
    runtime = object.__new__(MutekiRuntime)
    runtime._run_id = graph.challenge_id
    runtime._graph = graph
    return runtime


def _fact_texts(graph: MutekiGraph) -> list[str]:
    return [str(item.content) for item in graph.facts()]


def _finished_events(graph: MutekiGraph) -> list:
    return [
        item
        for item in graph.events_since()
        if item.event_type == EventType.RUN_FINISHED
    ]


def _convergence_directives(graph: MutekiGraph) -> list:
    return [
        item
        for item in graph.events_since()
        if item.event_type == EventType.COORDINATOR_DIRECTIVE
        and str(item.payload.get("directive")) == "stop_convergence"
    ]


def _verifying_runner(graph: MutekiGraph, started: asyncio.Event, delay: float = 0.25):
    """A Worker whose result lands *only* if the round is allowed to finish."""

    async def runner(job):
        started.set()
        await asyncio.sleep(delay)
        graph.add_fact(
            actor=job.worker_id,
            content=_PROTECTED_FACT,
            verified=True,
        )
        return WorkerOutcome(job.worker_id, "COMPLETED")

    return runner


class _IdlePool:
    """Pool stub with nothing running: the window must cost nothing."""

    active_count = 0

    async def wait(self) -> None:
        return None


class _BusyPool:
    """Pool stub with a round that never finishes on its own."""

    active_count = 1

    async def wait(self) -> None:
        await asyncio.Event().wait()


def _skeleton_coordinator(graph: MutekiGraph, pool, budget: float) -> MutekiCoordinator:
    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = graph
    coordinator.pool = pool
    coordinator.config = CoordinatorConfig(stop_convergence_seconds=budget)
    return coordinator


# ── unit: when the window is (and is not) granted ────────────────────────


def test_a_bare_stop_does_not_open_a_convergence_window(tmp_path) -> None:
    """No convergence was requested, so Finalize must not wait at all."""

    graph = _graph(tmp_path, "bare-stop")
    coordinator = _skeleton_coordinator(graph, _BusyPool(), 120.0)

    summary = asyncio.run(coordinator._await_stop_convergence("MUTEKI_RUN_TIMEOUT"))

    assert summary["convergence_waited_seconds"] == 0.0
    assert summary["convergence_rounds"] == 0
    assert _convergence_directives(graph) == []


def test_convergence_costs_nothing_when_the_pool_is_already_idle(tmp_path) -> None:
    """A granted window on an empty pool still finishes immediately."""

    graph = _graph(tmp_path, "idle-pool")
    coordinator = _skeleton_coordinator(graph, _IdlePool(), 120.0)
    coordinator.request_convergence("MUTEKI_RUN_CONVERGING", 120.0)

    started = time.monotonic()
    summary = asyncio.run(coordinator._await_stop_convergence("MUTEKI_RUN_TIMEOUT"))

    assert time.monotonic() - started < 0.5
    assert summary["convergence_rounds"] == 0


def test_caller_budget_shrinks_the_window_below_the_configured_cap(tmp_path) -> None:
    """The Runtime knows how much of the deadline it actually set aside."""

    graph = _graph(tmp_path, "shrunk-window")
    coordinator = _skeleton_coordinator(graph, _BusyPool(), 120.0)

    before = time.monotonic()
    coordinator.request_convergence("MUTEKI_RUN_CONVERGING", 0.05)
    granted = coordinator._converge_deadline - before

    assert granted == pytest.approx(0.05, abs=0.02)


def test_budget_is_clamped_to_the_finalize_safe_cap(tmp_path) -> None:
    """A Run cannot spend more on convergence than Finalize can afford."""

    graph = _graph(tmp_path, "clamped-window")
    coordinator = _skeleton_coordinator(graph, _BusyPool(), 3600.0)

    before = time.monotonic()
    coordinator.request_convergence("MUTEKI_RUN_CONVERGING")
    granted = coordinator._converge_deadline - before

    assert granted == pytest.approx(muteki_runtime.RUN_CONVERGENCE_SECONDS, abs=1.0)


# ── behaviour: the window changes what survives the deadline ─────────────


async def test_inflight_round_finishes_under_convergence_and_is_recorded(tmp_path) -> None:
    """The whole point: the round that was verifying gets to land its result."""

    graph = _graph(tmp_path, "converge-landed")
    graph.propose_intent(actor="reason", description="probe the target", payload={})
    started = asyncio.Event()

    orchestrator = _orchestrator(
        graph,
        _verifying_runner(graph, started, delay=0.25),
    )
    run_task = asyncio.create_task(orchestrator.run(max_rounds=None))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        orchestrator.coordinator.request_convergence("MUTEKI_RUN_CONVERGING", 5.0)
        await asyncio.wait_for(run_task, timeout=20)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert _PROTECTED_FACT in _fact_texts(graph)


async def test_hard_stop_still_reaps_the_round(tmp_path) -> None:
    """The negative case: a plain stop must stay fast and reaping."""

    graph = _graph(tmp_path, "hard-stop")
    graph.propose_intent(actor="reason", description="probe the target", payload={})
    started = asyncio.Event()

    orchestrator = _orchestrator(
        graph,
        _verifying_runner(graph, started, delay=0.5),
    )
    run_task = asyncio.create_task(orchestrator.run(max_rounds=None))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        orchestrator.coordinator.request_stop("TEST_STOP")
        await asyncio.wait_for(run_task, timeout=20)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert _PROTECTED_FACT not in _fact_texts(graph)


async def test_run_finished_reports_the_convergence_window(tmp_path) -> None:
    """The terminal record has to show that a window was spent, and on what."""

    graph = _graph(tmp_path, "converge-reported")
    graph.propose_intent(actor="reason", description="probe the target", payload={})
    started = asyncio.Event()

    orchestrator = _orchestrator(
        graph,
        _verifying_runner(graph, started, delay=0.2),
    )
    run_task = asyncio.create_task(orchestrator.run(max_rounds=None))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        orchestrator.coordinator.request_convergence("MUTEKI_RUN_CONVERGING", 5.0)
        await asyncio.wait_for(run_task, timeout=20)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    finished = _finished_events(graph)
    assert len(finished) == 1
    payload = finished[0].payload
    assert float(payload["convergence_waited_seconds"]) > 0
    assert _convergence_directives(graph)


# ── behaviour: the Runtime converges before it stops ─────────────────────


async def test_run_deadline_requests_convergence_before_it_stops(tmp_path) -> None:
    """The deadline must be spent in two stages, convergence first."""

    graph = _graph(tmp_path, "deadline-converge")

    async def runner(_job):
        await asyncio.Event().wait()

    orchestrator = _orchestrator(graph, runner)
    calls: list[tuple[str, float | None]] = []
    original = orchestrator.coordinator.request_convergence

    def spy(reason: str = "CONVERGING", budget_seconds: float | None = None) -> None:
        calls.append((reason, budget_seconds))
        original(reason, budget_seconds)

    orchestrator.coordinator.request_convergence = spy  # type: ignore[method-assign]

    result = await asyncio.wait_for(
        _runtime_boundary(graph)._run_orchestrator_with_deadline(
            orchestrator,
            max_rounds=None,
            total_timeout=0.4,
        ),
        timeout=20,
    )

    assert result.status == "TIMEOUT"
    assert result.reason == "MUTEKI_RUN_TIMEOUT"
    assert calls, "the deadline must request convergence before the hard stop"
    assert calls[0][0] == "MUTEKI_RUN_CONVERGING"
    # A quarter of the Run budget, never more than the configured ceiling.
    assert float(calls[0][1] or 0.0) == pytest.approx(0.1, abs=0.02)


async def test_run_deadline_returns_early_when_the_run_finishes_during_convergence(
    tmp_path,
) -> None:
    """Convergence is a window, not a delay: a Run that finishes returns at once."""

    graph = _graph(tmp_path, "converge-early-return")
    graph.propose_intent(actor="reason", description="probe the target", payload={})

    async def runner(job):
        await asyncio.sleep(0.05)
        graph.flag_found(actor=job.worker_id, flag="flag{converged}")
        return WorkerOutcome(job.worker_id, "COMPLETED", flag_found=True)

    orchestrator = _orchestrator(graph, runner)

    started = time.monotonic()
    result = await asyncio.wait_for(
        _runtime_boundary(graph)._run_orchestrator_with_deadline(
            orchestrator,
            max_rounds=None,
            total_timeout=8.0,
        ),
        timeout=20,
    )

    # The Run solved inside the decision stage, so the deadline never fired.
    assert time.monotonic() - started < 8.0
    assert result.status != "TIMEOUT"
