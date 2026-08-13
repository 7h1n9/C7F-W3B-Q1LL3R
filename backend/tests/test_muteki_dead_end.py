from __future__ import annotations

import asyncio

from app.solver.muteki import (
    EngineProfile,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
    WorkerOutcome,
    WorkerResultCode,
)
from app.solver.muteki.coordinator import MutekiCoordinator
from app.solver.muteki.outcomes import (
    DeadEndKind,
    DeadEndSignal,
    extract_dead_end_reasons,
    signal_from_worker_text,
)
from app.solver.muteki.worker.official_worker import OfficialWorkerResult
from app.solver.muteki.workers import WorkerJob


def _job(graph: MutekiGraph, worker_id: str, *, intent_id: str = "intent-1") -> WorkerJob:
    return WorkerJob(
        worker_id=worker_id,
        role="explore",
        engine_id="codex",
        graph_path=str(graph.db_path),
        challenge_id=graph.challenge_id,
        intent_id=intent_id,
        goal="test one route",
        payload={"tool_name": "http_request", "route_hash": "web:test:route"},
    )


def test_explicit_deadend_is_persisted_once_and_concludes_route(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "dead-end.sqlite", challenge_id="dead-end-run")
    signal = signal_from_worker_text(
        "DEADEND=the authenticated route returned no object-level access",
        payload={"route_hash": "web:idor:tickets"},
        intent_id="intent-1",
    )
    assert signal is not None
    assert signal.kind is DeadEndKind.ROUTE_DEAD_END

    async def runner(job: WorkerJob) -> WorkerOutcome:
        return WorkerOutcome(
            job.worker_id,
            "COMPLETED",
            result_code=WorkerResultCode.ROUTE_DEAD_END.value,
            dead_end=signal,
        )

    async def scenario() -> tuple[WorkerOutcome | BaseException, ...]:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        await pool.spawn(_job(graph, "worker-1"))
        first = await pool.wait()
        # A retry of the same route must not append a second route dead-end.
        pool._tasks.clear()
        pool._jobs.clear()
        await pool.spawn(_job(graph, "worker-2", intent_id="intent-2"))
        second = await pool.wait()
        return first + second

    try:
        outcomes = asyncio.run(scenario())
        assert len(graph.dead_ends()) == 1
        assert "route=web:idor:tickets" in graph.dead_ends()[0].description
        assert all(isinstance(item, WorkerOutcome) for item in outcomes)
    finally:
        graph.close()


def test_worker_exception_is_retryable_failure_not_target_deadend(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "worker-failure.sqlite", challenge_id="worker-failure")

    async def runner(_job: WorkerJob) -> WorkerOutcome:
        raise TimeoutError("provider timed out")

    async def scenario() -> tuple[WorkerOutcome | BaseException, ...]:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        await pool.spawn(_job(graph, "worker-1"))
        return await pool.wait()

    try:
        outcomes = asyncio.run(scenario())
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], WorkerOutcome)
        assert outcomes[0].result_code == WorkerResultCode.WORKER_FAILURE.value
        assert graph.dead_ends() == []
    finally:
        graph.close()


def test_deadend_marker_is_explicit_and_secret_safe() -> None:
    assert extract_dead_end_reasons("ordinary dead end prose") == ()
    signal = signal_from_worker_text(
        "DEADEND=route disproved; token=super-secret-token-value",
        payload={"route_hash": "web:secret:test"},
    )
    assert signal is not None
    assert "super-secret" not in signal.reason
    assert "[redacted]" in signal.reason


def test_coordinator_reason_invocation_is_auditable(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "reason-events.sqlite", challenge_id="reason-events")
    calls: list[int] = []
    graph.add_fact(actor="test", content="initial board revision", verified=False)

    def provider(snapshot):
        calls.append(int(snapshot["revision"]))
        return '{"verdict":"course_correct","goal_met":false,"drift":"use review","intents":[]}'

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(provider),
            pool,
            [EngineProfile("codex")],
            config={"race_enabled": False, "interval_seconds": 0, "review_interval": 0},
        )
        await coordinator.run(max_ticks=1)

    try:
        asyncio.run(scenario())
        events = graph.events_since()
        reason_events = [event.event_type for event in events if event.event_type.startswith("reason.")]
        assert calls
        assert reason_events == ["reason.started", "reason.completed"]
        completed = next(event for event in events if event.event_type == "reason.completed")
        assert completed.payload["verdict"] == "course_correct"
        assert completed.payload["intent_count"] == 0
    finally:
        graph.close()


def test_reason_failure_is_visible_and_does_not_crash_coordinator(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "reason-failure.sqlite", challenge_id="reason-failure")
    graph.add_fact(actor="test", content="initial board revision", verified=False)

    def provider(_snapshot):
        raise RuntimeError("reason provider unavailable")

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(provider),
            pool,
            [EngineProfile("codex")],
            config={"race_enabled": False, "interval_seconds": 0, "review_interval": 0},
        )
        await coordinator.run(max_ticks=1)

    try:
        asyncio.run(scenario())
        assert any(event.event_type == "reason.failed" for event in graph.events_since())
        assert graph.dead_ends() == []
    finally:
        graph.close()


def test_official_result_deadend_signal_uses_common_pool_boundary(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "official-dead-end.sqlite", challenge_id="official-dead-end")
    signal = DeadEndSignal(
        DeadEndKind.ROUTE_DEAD_END,
        "the route was disproved by the observed response",
        route_hash="web:route:one",
    )

    class OfficialWorker:
        async def execute(self, _job):
            return OfficialWorkerResult(
                False,
                "COMPLETED",
                "codex",
                dead_end_signal=signal,
            )

    try:
        # Exercise the actual official runner, then apply the returned signal
        # through the same pool boundary used by production execution.
        async def run_pool() -> None:
            coordinator = object.__new__(MutekiCoordinator)
            coordinator.official_worker = OfficialWorker()
            coordinator.graph = graph
            coordinator.official_worker_usage_bridge = None
            coordinator._native_event_cursor = lambda: None
            pool = MutekiWorkerPool(
                graph,
                lambda _job: None,
                max_workers=1,
                external_runner=coordinator._official_worker_runner,
                external_runner_exclusive=True,
            )
            await pool.spawn(_job(graph, "worker-1"))
            outcomes = await pool.wait()
            assert isinstance(outcomes[0], WorkerOutcome)
            assert len(graph.dead_ends()) == 1

        asyncio.run(run_pool())
    finally:
        graph.close()


__all__ = ["DeadEndSignal"]
