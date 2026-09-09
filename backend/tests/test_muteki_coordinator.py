from __future__ import annotations

import asyncio

from app.solver.muteki import (
    CoordinatorConfig,
    EngineProfile,
    EventType,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
)
from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.core.orchestrator import MutekiOrchestrator
from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime
from app.solver.muteki.worker.official_worker import OfficialWorkerResult
from app.solver.muteki.workers import WorkerJob


def test_race_flag_takes_fast_path_and_finalize_is_single_terminal_event(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1")

    async def runner(job):
        graph.write_flag(actor=job.worker_id, flag="flag{race-win}", real_output="real command output flag{race-win}")
        return None

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=3)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("claude"), EngineProfile("unhealthy", healthy=False)],
        )
        await coordinator.run(max_ticks=0)

    asyncio.run(scenario())
    assert graph.flags(verified_only=True)
    event_types = [item.event_type for item in graph.events_since()]
    assert event_types.count(EventType.RUN_FINISHED) == 1
    assert any(item.event_type == EventType.PHASE_CHANGED and item.payload["phase"] == "race" for item in graph.events_since())


def test_coordinator_reason_writes_one_dispatchable_intent_after_race(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1")

    async def runner(job):
        if job.intent_id:
            assert any(item.status == "claimed" and item.claimed_by == job.worker_id for item in graph.intents())
            graph.add_fact(actor=job.worker_id, content="worker observed a bounded endpoint", verified=True)
            assert graph.conclude_intent(actor=job.worker_id, intent_id=job.intent_id, result="SUCCESS")

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=2)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _: [{"goal": "inspect the next endpoint", "worker_class": "code"}]),
            pool,
            [EngineProfile("codex")],
        )
        await coordinator.run(max_ticks=1)

    asyncio.run(scenario())
    assert graph.facts(verified_only=True)
    assert any(item.description == "inspect the next endpoint" for item in graph.intents())


def test_race_timeout_recovers_into_coordinator_instead_of_terminating_run(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-timeout")

    async def runner(job):
        await asyncio.sleep(10)

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex")],
            config=CoordinatorConfig(worker_timeout_seconds=1, worker_wait_grace_seconds=0),
        )
        await coordinator.run(max_ticks=1)
        assert coordinator.stop_reason is None
        assert any(item.description == "RACE_WORKER_TIMEOUT" for item in graph.dead_ends())
        phases = [
            str(item.payload.get("phase"))
            for item in graph.events_since()
            if item.event_type == EventType.PHASE_CHANGED
        ]
        assert phases == ["prepare", "race", "coordinator", "finalize"]

    asyncio.run(scenario())


def test_run_timeout_cooperatively_finalizes_before_returning_timeout(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "run-timeout.sqlite", challenge_id="challenge-run-timeout")
    worker_finished = False

    async def runner(_job):
        nonlocal worker_finished
        await asyncio.sleep(0.05)
        worker_finished = True

    async def scenario():
        orchestrator = MutekiOrchestrator(
            graph,
            MutekiReason(),
            worker_runner=runner,
            engines=[EngineProfile("gateway-runner")],
            interval_seconds=0.0,
            worker_timeout_seconds=1,
        )
        runtime = object.__new__(MutekiRuntime)
        runtime._run_id = graph.challenge_id
        runtime._graph = graph
        return await runtime._run_orchestrator_with_deadline(
            orchestrator,
            max_rounds=None,
            total_timeout=0.01,
        )

    result = asyncio.run(scenario())
    events = graph.events_since()
    phases = [
        str(item.payload.get("phase"))
        for item in events
        if item.event_type == EventType.PHASE_CHANGED
    ]
    finished = [
        item
        for item in events
        if item.event_type == EventType.RUN_FINISHED
    ]

    assert worker_finished
    assert result.status == "TIMEOUT"
    assert result.reason == "MUTEKI_RUN_TIMEOUT"
    assert not result.flag_found
    assert phases == ["prepare", "race", "finalize"]
    assert len(finished) == 1
    assert finished[0].payload["reason"] == "MUTEKI_RUN_TIMEOUT"
    assert finished[0].payload["flag_found"] is False


def test_total_worker_budget_matches_upstream_spawn_boundary(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "budget.sqlite", challenge_id="challenge-budget")
    spawned: list[str] = []

    async def runner(job):
        spawned.append(job.worker_id)

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _: [{"goal": "try another bounded route", "worker_class": "code"}]),
            pool,
            [EngineProfile("codex")],
            config=CoordinatorConfig(
                race_enabled=False,
                interval_seconds=0,
                max_workers=1,
                max_total_workers=1,
            ),
        )
        await coordinator.run(max_ticks=4)

    asyncio.run(scenario())
    assert spawned == ["worker-1"]
    assert any(item.description == "WORKER_BUDGET_EXHAUSTED" for item in graph.dead_ends())


def test_zero_interval_coordinator_yields_to_new_worker_before_finalize(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1")
    executed = []

    async def runner(job):
        executed.append(job.worker_id)
        graph.add_fact(actor=job.worker_id, content="worker completed", verified=True)

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _: [{"goal": "inspect endpoint", "worker_class": "code"}]),
            pool,
            [EngineProfile("gateway-runner")],
            config={"interval_seconds": 0.0, "max_workers": 1},
        )
        await coordinator.run(max_ticks=2)

    asyncio.run(scenario())
    assert executed and executed[0] == "worker-1"
    events = graph.events_since()
    assert sum(item.event_type == EventType.WORKER_STARTED for item in events) == sum(item.event_type == EventType.WORKER_FINISHED for item in events)


def test_coordinator_waits_for_delayed_worker_before_consuming_tick_budget(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1")
    completed = []

    async def runner(job):
        await asyncio.sleep(0.02)
        completed.append(job.worker_id)
        graph.add_fact(actor=job.worker_id, content="delayed worker completed", verified=True)

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _: [{"goal": "inspect delayed endpoint", "worker_class": "code"}]),
            pool,
            [EngineProfile("gateway-runner")],
            config={"interval_seconds": 0.0, "max_workers": 1},
        )
        await coordinator.run(max_ticks=1)

    asyncio.run(scenario())
    assert "worker-2" in completed


def test_coordinator_allows_worker_result_reporting_grace(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1")
    completed = []

    async def runner(job):
        await asyncio.sleep(1.02)
        completed.append(job.worker_id)
        graph.add_fact(actor=job.worker_id, content="worker completed at deadline boundary", verified=True)

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _: [{"goal": "inspect deadline boundary", "worker_class": "code"}]),
            pool,
            [EngineProfile("gateway-runner")],
            config={
                "interval_seconds": 0.0,
                "max_workers": 1,
                "race_enabled": False,
                "worker_timeout_seconds": 1,
                "worker_wait_grace_seconds": 0.2,
            },
        )
        await coordinator.run(max_ticks=1)

    asyncio.run(scenario())
    assert completed == ["worker-1"]
    assert not any(item.description == "WORKER_TIMEOUT" for item in graph.dead_ends())


def test_coordinator_can_skip_race_when_disabled(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-no-race")
    phases: list[str] = []

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("gateway-runner")],
            config={"race_enabled": False, "max_ticks": 0},
        )
        await coordinator.run(max_ticks=0)

    asyncio.run(scenario())
    phases.extend(
        str(item.payload.get("phase"))
        for item in graph.events_since()
        if item.event_type == EventType.PHASE_CHANGED
    )
    assert phases == ["prepare", "coordinator", "finalize"]


def test_course_correct_verdict_routes_through_review_worker(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "course-correct.sqlite", challenge_id="challenge-course-correct")
    review_jobs = []

    async def review_handler(job):
        review_jobs.append((job.role, job.goal))

    async def scenario() -> None:
        pool = MutekiWorkerPool(
            graph,
            lambda _job: None,
            max_workers=1,
            review_handler=review_handler,
        )
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(
                lambda _snapshot: (
                    '{"verdict":"course_correct","goal_met":false,'
                    '"drift":"use a different evidence-backed route",'
                    '"intents":[]}'
                )
            ),
            pool,
            [EngineProfile("gateway-runner")],
            config={"race_enabled": False, "review_interval": 0, "max_workers": 1},
        )
        await coordinator.run(max_ticks=1)

    try:
        asyncio.run(scenario())
        assert review_jobs == [("review", "review course-corrected route")]
        assert any(
            item.event_type == EventType.COORDINATOR_DIRECTIVE
            and item.payload == {"directive": "course_correct", "review_requested": True}
            for item in graph.events_since()
        )
    finally:
        graph.close()


def test_external_worker_owns_official_bootstrap_without_intent(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "bootstrap.sqlite", challenge_id="challenge-bootstrap")
    seen: list[tuple[str, str | None]] = []

    async def external_runner(job):
        seen.append((job.role, job.intent_id))

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        pool.external_runner = external_runner
        assert await pool.spawn(
            WorkerJob(
                worker_id="bootstrap-1",
                role="bootstrap",
                engine_id="codex",
                graph_path=str(graph.db_path),
                challenge_id=graph.challenge_id,
            )
        )
        await pool.wait()

    asyncio.run(scenario())
    assert seen == [("bootstrap", None)]


def test_external_worker_owns_official_race_without_intent(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "race-native.sqlite", challenge_id="challenge-race-native")
    seen: list[tuple[str, str | None]] = []

    async def external_runner(job):
        seen.append((job.role, job.intent_id))

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        pool.external_runner = external_runner
        assert await pool.spawn(
            WorkerJob(
                worker_id="race-1",
                role="race",
                engine_id="codex",
                graph_path=str(graph.db_path),
                challenge_id=graph.challenge_id,
            )
        )
        await pool.wait()

    asyncio.run(scenario())
    assert seen == [("race", None)]


def test_exclusive_external_worker_never_falls_back_for_local_engine(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "container-only.sqlite", challenge_id="challenge-container-only")
    seen: list[str] = []

    async def external_runner(job):
        seen.append(job.engine_id)

    async def local_runner(_job):
        raise AssertionError("compatibility callback must not run")

    async def scenario() -> None:
        pool = MutekiWorkerPool(
            graph,
            local_runner,
            max_workers=1,
            external_runner=external_runner,
            external_runner_exclusive=True,
        )
        assert await pool.spawn(
            WorkerJob(
                worker_id="openai-1",
                role="explore",
                engine_id="openai-compatible:model-1",
                graph_path=str(graph.db_path),
                challenge_id=graph.challenge_id,
                intent_id="intent-1",
            )
        )
        await pool.wait()

    asyncio.run(scenario())
    assert seen == ["openai-compatible:model-1"]
    graph.close()


def test_official_rebootstrap_is_not_silently_capped_at_one(tmp_path) -> None:
    """A barren official turn must leave the Coordinator another route."""

    graph = UpstreamRuntimeGraph(
        tmp_path / "rebootstrap.sqlite",
        challenge=type(
            "Challenge",
            (),
            {
                "id": "rebootstrap-challenge",
                "name": "rebootstrap",
                "challenge_type": "WEB_TARGET",
                "description": "",
                "target_url": "http://target.test",
                "flag_pattern": r"flag\{[^}]+\}",
            },
        )(),
        challenge_id="rebootstrap-run",
    )
    attempts: list[str] = []

    class FakeOfficialWorker:
        async def start(self, **_kwargs):
            return None

        async def execute(self, job):
            attempts.append(job.role)
            return OfficialWorkerResult(
                success=False,
                status="FAILED",
                engine=job.engine_id,
                metadata={"reason": "bounded test failure"},
            )

        async def stop(self):
            return None

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _snapshot: []),
            pool,
            [EngineProfile("codex")],
            config={
                "worker_backend": "upstream_container",
                "race_enabled": False,
                "interval_seconds": 0.0,
                "review_interval": 0,
                "max_workers": 1,
            },
            official_worker_adapter=FakeOfficialWorker(),
        )
        await coordinator.run(max_ticks=3)

    try:
        asyncio.run(scenario())
        assert attempts.count("bootstrap") == 2
        assert attempts[0] == "explore"
    finally:
        graph.close()
