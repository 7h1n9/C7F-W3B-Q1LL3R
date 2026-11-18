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


def test_race_stops_waiting_once_a_flag_is_verified(tmp_path) -> None:
    """A solved Race must not wait for its slowest lane.

    Measured 2026-10-19: the A-path lane finished in ~2 of the 12 minutes and
    the CLI lane used the rest, so every Run paid the full Worker budget after
    the flag was already on the Blackboard.
    """

    graph = MutekiGraph(tmp_path / "race-early-exit.db", challenge_id="challenge-race-early")

    async def runner(job):
        if job.worker_id == "worker-1":
            graph.write_flag(
                actor=job.worker_id,
                flag="flag{race-win}",
                real_output="real command output flag{race-win}",
            )
            return None
        # Still probing.  It cannot make the verified flag above less true.
        await asyncio.sleep(30)

    async def scenario() -> None:
        # ``external_runner`` is what keeps both lanes: without it ``_race``
        # deliberately narrows a compatibility pool to one scout, and a
        # single-lane Race cannot demonstrate anything about waiting for the
        # slowest lane.
        pool = MutekiWorkerPool(graph, runner, max_workers=2, external_runner=runner)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("claude")],
            config=CoordinatorConfig(worker_timeout_seconds=20, worker_wait_grace_seconds=0),
        )
        # 20s Worker window: anything above 10s means the Race waited out the
        # slow lane instead of ending on the verified flag.
        await asyncio.wait_for(coordinator.run(max_ticks=0), timeout=10)

    asyncio.run(scenario())
    assert graph.flags(verified_only=True)
    assert any(
        item.event_type == EventType.PHASE_CHANGED and item.payload["phase"] == "finalize"
        for item in graph.events_since()
    )


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

    async def runner(_job):
        # Deliberately far longer than the Run-level deadline.  A deadline is a
        # stop boundary, not a Worker deadline: the in-flight Race Worker is
        # reaped by Finalize instead of being waited out for the rest of its
        # window, which is what used to delay the terminal handoff.
        await asyncio.sleep(30)

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

    # The bound is what proves the deadline did not wait out the 30s Worker.
    result = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
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

    assert result.status == "TIMEOUT"
    assert result.reason == "MUTEKI_RUN_TIMEOUT"
    assert not result.flag_found
    assert phases == ["prepare", "race", "finalize"]
    assert len(finished) == 1
    assert finished[0].payload["reason"] == "MUTEKI_RUN_TIMEOUT"
    assert finished[0].payload["flag_found"] is False


def test_stop_request_interrupts_long_worker_wait_and_finalizes(tmp_path) -> None:
    """A stop request must be observed in seconds, not at the next tick."""

    graph = MutekiGraph(tmp_path / "stop-latency.sqlite", challenge_id="challenge-stop-latency")

    async def scenario() -> tuple[float, bool, str | None]:
        worker_running = asyncio.Event()
        worker_finished = False

        async def runner(_job):
            nonlocal worker_finished
            worker_running.set()
            await asyncio.sleep(30)  # a real Worker window, far beyond the stop
            worker_finished = True

        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _snapshot: [{"goal": "one long bounded route", "worker_class": "code"}]),
            pool,
            [EngineProfile("gateway-runner")],
            config=CoordinatorConfig(
                race_enabled=False,
                interval_seconds=0.0,
                max_workers=1,
                worker_timeout_seconds=30,
                worker_wait_grace_seconds=0.0,
            ),
        )
        task = asyncio.create_task(coordinator.run(max_ticks=1))
        await worker_running.wait()
        await asyncio.sleep(0.05)
        started = asyncio.get_running_loop().time()
        coordinator.request_stop("MUTEKI_RUN_TIMEOUT")
        await asyncio.wait_for(task, timeout=5)
        return (
            asyncio.get_running_loop().time() - started,
            worker_finished,
            coordinator.stop_reason,
        )

    elapsed, worker_finished, stop_reason = asyncio.run(scenario())
    events = graph.events_since()
    finished = [item for item in events if item.event_type == EventType.RUN_FINISHED]

    # The Worker still had ~30s of its window left when the stop was requested.
    assert not worker_finished
    assert elapsed < 5
    assert stop_reason == "MUTEKI_RUN_TIMEOUT"
    assert len(finished) == 1
    assert finished[0].payload["reason"] == "MUTEKI_RUN_TIMEOUT"
    assert finished[0].payload["flag_found"] is False
    # An interrupted round must not be mislabeled, and must not be charged as a
    # barren Worker round (that path would overwrite the stop reason).
    assert not any(item.description == "WORKER_TIMEOUT" for item in graph.dead_ends())
    assert not any(
        item.description.startswith("BARREN_LIMIT") for item in graph.dead_ends()
    )


def test_finalize_survives_cancellation_and_emits_run_finished(tmp_path) -> None:
    """Cancelling the Run task inside Finalize must not lose RUN_FINISHED."""

    graph = MutekiGraph(tmp_path / "finalize-cancel.sqlite", challenge_id="challenge-finalize-cancel")

    async def scenario() -> tuple[bool, bool, bool]:
        in_finalize = asyncio.Event()
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("gateway-runner")],
            config=CoordinatorConfig(race_enabled=False, interval_seconds=0.0, max_workers=1),
        )
        original_cancel_all = pool.cancel_all

        async def slow_cancel_all() -> None:
            in_finalize.set()
            await asyncio.sleep(0.3)  # a genuinely in-flight terminal step
            await original_cancel_all()

        pool.cancel_all = slow_cancel_all
        task = asyncio.create_task(coordinator.run(max_ticks=0))
        await in_finalize.wait()
        task.cancel()
        await asyncio.sleep(0.05)
        cancelled_inside_finalize = not task.done()
        task.cancel()  # a second cancel must not be able to abort the handoff
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        cancelled = task.cancelled()
        failed = (not cancelled) and task.exception() is not None
        # A repeat Finalize must stay idempotent: one Run, one terminal event.
        await coordinator.finalize(reason="STOPPED")
        return cancelled_inside_finalize, cancelled, failed

    cancelled_inside_finalize, cancelled, failed = asyncio.run(scenario())
    events = graph.events_since()
    finished = [item for item in events if item.event_type == EventType.RUN_FINISHED]

    assert cancelled_inside_finalize
    assert not cancelled
    assert not failed
    assert len(finished) == 1
    assert finished[0].payload["reason"] == "STOPPED"
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


def test_a_transient_healthcheck_failure_does_not_abandon_the_run(tmp_path, monkeypatch) -> None:
    """One bad upstream moment must not kill a Run before its first Intent.

    The prepare-phase probe is a real model turn per engine, so a relay that
    briefly returns 4xx/5xx looks identical to a dead engine.  A single-pass gate
    turned that blip into NO_HEALTHY_WORKER_PROFILE with zero work done.
    """

    from app.solver.muteki import coordinator as coordinator_module

    # Keep the test fast; the production delay and window are deliberate.
    monkeypatch.setattr(coordinator_module, "HEALTHCHECK_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(coordinator_module, "HEALTHCHECK_MAX_WAIT_SECONDS", 30.0)

    graph = UpstreamRuntimeGraph(
        tmp_path / "healthcheck.sqlite",
        challenge=type(
            "Challenge",
            (),
            {
                "id": "hc-challenge",
                "name": "hc",
                "challenge_type": "WEB_TARGET",
                "description": "",
                "target_url": "http://target.test",
                "flag_pattern": r"flag\{[^}]+\}",
            },
        )(),
        challenge_id="hc-run",
    )

    class FlakyOfficialWorker:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def start(self, **_kwargs):
            return None

        async def health(self, profile, *, workspace):
            seen = self.calls.get(profile.engine_id, 0) + 1
            self.calls[profile.engine_id] = seen
            # First probe of each engine fails; every later probe succeeds.
            return (seen > 1), ("" if seen > 1 else "WORKER_HEALTHCHECK_FAILED")

        async def stop(self):
            return None

    worker = FlakyOfficialWorker()

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _snapshot: []),
            pool,
            [EngineProfile("codex", driver_profile={"protocol": "codex_cli"})],
            config={"worker_backend": "upstream_container", "max_workers": 1},
            official_worker_adapter=worker,
        )
        await coordinator._prepare()
        assert coordinator._stop_requested is False
        assert coordinator._unavailable_engine_ids == set()

    try:
        asyncio.run(scenario())
        assert worker.calls["codex"] == 2
    finally:
        graph.close()


def test_a_persistently_unhealthy_roster_still_fails_closed(tmp_path, monkeypatch) -> None:
    from app.solver.muteki import coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "HEALTHCHECK_RETRY_DELAY_SECONDS", 0.0)
    # A zero window means "one pass only", which is the behaviour under test.
    monkeypatch.setattr(coordinator_module, "HEALTHCHECK_MAX_WAIT_SECONDS", 0.0)

    graph = UpstreamRuntimeGraph(
        tmp_path / "healthcheck.sqlite",
        challenge=type(
            "Challenge",
            (),
            {
                "id": "hc-challenge",
                "name": "hc",
                "challenge_type": "WEB_TARGET",
                "description": "",
                "target_url": "http://target.test",
                "flag_pattern": r"flag\{[^}]+\}",
            },
        )(),
        challenge_id="hc-run",
    )

    class DeadOfficialWorker:
        def __init__(self) -> None:
            self.calls = 0

        async def start(self, **_kwargs):
            return None

        async def health(self, profile, *, workspace):
            self.calls += 1
            return False, "WORKER_HEALTHCHECK_FAILED"

        async def stop(self):
            return None

    worker = DeadOfficialWorker()

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _snapshot: []),
            pool,
            [EngineProfile("codex", driver_profile={"protocol": "codex_cli"})],
            config={"worker_backend": "upstream_container", "max_workers": 1},
            official_worker_adapter=worker,
        )
        await coordinator._prepare()
        assert coordinator._stop_requested is True
        assert coordinator._stop_reason == "NO_HEALTHY_WORKER_PROFILE"

    try:
        asyncio.run(scenario())
        # A zero retry window means exactly one pass, never an endless probe.
        assert worker.calls == 1
    finally:
        graph.close()


def test_backfill_goal_fits_the_intent_limit_and_names_the_pivot() -> None:
    """The single-Worker goal must survive ``intent_goal[:600]`` intact.

    ``muteki_runtime`` truncates the goal at 600 chars.  The previous template
    was already long enough to lose its own closing instruction, and the backfill
    prefix is prepended on top -- so the budget is a real contract, not a style
    preference: a truncated goal silently drops the "don't burn the window on
    robots.txt" sentence exactly when one engine is carrying two lanes.
    """

    from app.solver.muteki.coordinator import _BACKFILL_GOAL_PREFIX, _SINGLE_WORKER_GOAL

    for missing in (1, 2, 3):
        goal = _BACKFILL_GOAL_PREFIX.format(missing=missing) + _SINGLE_WORKER_GOAL
        assert len(goal) <= 600, f"backfill goal for {missing} engines is {len(goal)} chars"
        assert goal.startswith("[BACKFILL]")
        assert "you also own their lane" in goal
        # The pivot instruction is the whole point: without it the surviving
        # engine repeats the failed exploit until the window closes.
        assert "pivot to public endpoints" in goal
        # The closing instruction must survive the 600-char cut.  It now bans the
        # fingerprint pass from repeating rather than "login crawling", because
        # the fingerprint step was added to the front of the goal and the ban had
        # to narrow to what is genuinely dead weight.
        assert goal.endswith("do not repeat the fingerprint.")


def test_backfill_event_type_is_declared() -> None:
    assert EventType.ENGINE_BACKFILL == "engine.backfill"


def test_cycle_end_challenges_unverified_facts_and_concludes_unclaimed_intents(tmp_path) -> None:
    """Nothing unverified and nothing unowned may cross into the next cycle.

    Two failures used to compound here: a Worker's self-report counted as
    progress, and an Intent nobody claimed stayed ``open`` so the next cycle
    inherited the same direction.  This pins the end-of-cycle gate for both.
    """

    graph = MutekiGraph(tmp_path / "g.db", challenge_id="challenge-1")
    graph.add_fact(actor="worker-1", content="achieved RCE as www-data, PWN42 executed")
    graph.add_fact(actor="host-verifier", content="port 80 is open", verified=True)
    unclaimed = graph.propose_intent(actor="coordinator", description="probe /admin")

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = graph
    coordinator._unverified_only_rounds = 2

    coordinator._close_cycle_semantics(graph.facts())

    challenged = [
        event
        for event in graph.events_since()
        if str(event.event_type) == str(EventType.FACT_CHALLENGED)
    ]
    assert len(challenged) == 1
    payload = challenged[0].payload
    # Only the unverified fact is challenged: the host-verified one is a premise.
    assert payload["count"] == 1
    assert payload["reason"] == "UNVERIFIED_SELF_REPORT"
    assert payload["unverified_only_rounds"] == 2

    # The unclaimed Intent was claimed by the Coordinator and concluded.
    assert graph.intents(status="open") == []
    concluded = [
        event
        for event in graph.events_since()
        if str(event.event_type) == str(EventType.INTENT_CONCLUDED)
        and event.payload.get("intent_id") == unclaimed
    ]
    assert concluded
    assert concluded[0].payload["result"] == "unclaimed_at_cycle_end"


def test_cycle_end_leaves_a_claimed_intent_alone(tmp_path) -> None:
    """Never steal a lease from a Worker that is actually running the Intent."""

    graph = MutekiGraph(tmp_path / "g2.db", challenge_id="challenge-2")
    intent_id = graph.propose_intent(actor="coordinator", description="probe /login")
    assert graph.claim_intent(worker="worker-1", intent_id=intent_id, lease_s=120)

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = graph
    coordinator._unverified_only_rounds = 1

    coordinator._close_cycle_semantics([])

    still_open = graph.intents(status="claimed")
    assert [item.intent_id for item in still_open] == [intent_id]


# ── the recipe's first move is injected into the lane goal ──────────────────


def test_the_exploit_lane_goal_carries_the_recipes_first_action(tmp_path) -> None:
    """A recipe the Worker can read is not a recipe it will follow.

    Measured on run ``50a60596`` (``[Solr]CVE-2019-0193``): the recipe was in
    ``AGENTS.md`` with the exact endpoint, the Worker ``cat``-ed the file, and
    the round still spent 71 requests on the Solr config API against 2 on the
    endpoint the recipe names.  "Read ./AGENTS.md" is an instruction a Worker can
    satisfy and then ignore; the goal is the one place it cannot file away as
    background reading.
    """

    graph = MutekiGraph(tmp_path / "goal.sqlite", challenge_id="challenge-goal")
    goals: list[tuple[str, str, str]] = []

    async def runner(job):
        goals.append((job.role, str(job.payload.get("race_lane") or ""), job.goal or ""))
        graph.write_flag(actor=job.worker_id, flag="flag{goal}", real_output="real output flag{goal}")

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=3)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("claude"), EngineProfile("unhealthy", healthy=False)],
            config={
                "exploit_first_action": "枚举 core：`GET /solr/admin/cores?action=STATUS&wt=json`",
            },
        )
        await coordinator.run(max_ticks=0)

    try:
        asyncio.run(scenario())
        exploit = [goal for _, lane, goal in goals if lane.startswith("exploit:")]
        assert exploit, goals
        assert "FIRST ACTION" in exploit[0]
        assert "/solr/admin/cores?action=STATUS" in exploit[0]
        # Recon lanes have different jobs; they must not inherit the recipe's move.
        for _, lane, goal in goals:
            if lane.startswith("recon:"):
                assert "FIRST ACTION" not in goal
    finally:
        graph.close()


def test_without_a_recipe_the_exploit_lane_goal_is_unchanged(tmp_path) -> None:
    """An unannotated box must behave exactly as it did before this hook."""

    graph = MutekiGraph(tmp_path / "goal-none.sqlite", challenge_id="challenge-no-recipe")
    goals: list[str] = []

    async def runner(job):
        goals.append(job.goal or "")
        graph.write_flag(actor=job.worker_id, flag="flag{none}", real_output="real output flag{none}")

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=3)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("claude"), EngineProfile("unhealthy", healthy=False)],
        )
        await coordinator.run(max_ticks=0)

    try:
        asyncio.run(scenario())
        assert goals
        assert all("FIRST ACTION" not in goal for goal in goals)
        assert any("Known exploit path" in goal for goal in goals)
    finally:
        graph.close()


def test_the_orchestrator_forwards_the_first_action_into_the_coordinator(tmp_path) -> None:
    """This constructor takes explicit parameters, so a new keyword must be listed.

    Getting it wrong is not a loud failure: the Runtime catches the TypeError and
    the Run dies as ``MUTEKI_RUNTIME_ERROR`` with ``status=FAILED_ENGINE`` and no
    traceback in the backend log.  That is exactly how the first attempt at this
    hook failed in production (run ``f3d6f77e``), which is why the forwarding is
    asserted here rather than left to an end-to-end Run.
    """

    from app.solver.muteki.core.orchestrator import MutekiOrchestrator

    graph = MutekiGraph(tmp_path / "forward.sqlite", challenge_id="challenge-forward")
    try:
        orchestrator = MutekiOrchestrator(
            graph,
            MutekiReason(),
            worker_runner=lambda _job: None,
            exploit_first_action="枚举 core：`GET /solr/admin/cores?action=STATUS`",
        )
        assert (
            orchestrator.coordinator.config.exploit_first_action
            == "枚举 core：`GET /solr/admin/cores?action=STATUS`"
        )
    finally:
        graph.close()


def test_the_orchestrator_leaves_the_field_empty_by_default(tmp_path) -> None:
    from app.solver.muteki.core.orchestrator import MutekiOrchestrator

    graph = MutekiGraph(tmp_path / "forward-default.sqlite", challenge_id="challenge-default")
    try:
        orchestrator = MutekiOrchestrator(
            graph, MutekiReason(), worker_runner=lambda _job: None
        )
        assert orchestrator.coordinator.config.exploit_first_action == ""
    finally:
        graph.close()


# ── the reactivation probe must not wait forever (2026-10-26) ───────────────


def test_the_reactivation_probe_gives_up_instead_of_polling_forever() -> None:
    """A probe with no ceiling degrades into a probe that never runs.

    Measured on run ``4666646b`` (``[CISCN2019 华东北赛区]Web2``): Claude failed
    its one PREPARE probe while the container was still warming up, Codex
    passed, and the health loop broke on the first pass because *something*
    answered.  Claude stayed in ``_unavailable_engine_ids`` for the whole Run --
    1124 seconds, 117 steps, one lane, ``retry_attempts: {"claude-cli": 0}``.

    ``_race`` does schedule the probe.  But it waits for ``pool.active_count``
    to reach zero before touching the Sandbox, and the surviving lane never
    released it, so the wait outlived the Run.  Re-running the identical Claude
    command by hand in that same container succeeded in 6s, which is what makes
    this a scheduling bug rather than a dead engine.

    A bounded wait is the honest trade: the second lane is a bonus, and losing
    the bonus beats holding a task that can never proceed.
    """

    import inspect

    from app.solver.muteki import coordinator as coordinator_module

    assert coordinator_module.HEALTHCHECK_IDLE_WAIT_SECONDS > 0
    # Shorter than the overall probe budget: waiting longer than the Run can
    # last is the failure this constant exists to prevent.
    assert coordinator_module.HEALTHCHECK_IDLE_WAIT_SECONDS <= coordinator_module.HEALTHCHECK_MAX_WAIT_SECONDS

    source = inspect.getsource(coordinator_module.MutekiCoordinator._retry_unavailable_engines)
    assert "HEALTHCHECK_IDLE_WAIT_SECONDS" in source
    # The idle loop must carry the deadline, not just the stop flag.
    assert "idle_deadline" in source


def test_the_idle_wait_is_still_a_wait_not_a_bypass() -> None:
    """Bounding the wait must not start probing alongside a live Worker.

    The one container cannot host two CLI processes; running the probe during a
    Worker round would corrupt that round's Sandbox.  The loop must still be
    gated on ``pool.active_count``, with the deadline only deciding when to stop
    waiting -- never whether to wait.
    """

    import inspect

    from app.solver.muteki import coordinator as coordinator_module

    source = inspect.getsource(coordinator_module.MutekiCoordinator._retry_unavailable_engines)
    while_at = source.find("while self.pool.active_count")
    assert while_at >= 0, "the probe must still wait for an idle Sandbox"
    # The deadline is checked inside that loop, after the gate.
    assert source.find("idle_deadline", while_at) > while_at


def test_injected_transient_failure_is_retried_by_the_real_adapter(monkeypatch) -> None:
    """The retry must be reachable on the real class, not only in a stub.

    ``MUTEKI_INJECT_HEALTH_FAILURE_ONCE`` exists because a real upstream wobble
    cannot be scheduled; without it the retry path had only ever been exercised
    by tests that replaced the probe wholesale.
    """

    import asyncio

    from app.solver.muteki.worker import official_worker as module

    monkeypatch.setattr(module, "_HEALTH_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setenv("MUTEKI_INJECT_HEALTH_FAILURE_ONCE", "1")
    # Fresh process-level state for the one-shot injection.
    module._HEALTH_INJECTION_STATE = {"fired": False}

    class Profile:
        engine_id = "codex"
        driver_profile = {"protocol": "codex_cli", "engine": "codex"}
        environment: dict = {}

    worker = module.OfficialWorkerAdapter.__new__(module.OfficialWorkerAdapter)
    real = module.OfficialWorkerAdapter._health_once
    calls = {"n": 0}

    async def stub(self, profile, *, workspace):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(self, profile, workspace=workspace)
        return True, ""

    monkeypatch.setattr(module.OfficialWorkerAdapter, "_health_once", stub)
    healthy, reason = asyncio.run(worker.health(Profile(), workspace="/tmp/ws"))

    assert healthy is True
    assert reason == ""
    assert calls["n"] == 2, "the transient failure must trigger exactly one retry"


def test_non_transient_failure_is_not_retried(monkeypatch) -> None:
    """A bad credential is a real fault; retrying it only burns the budget."""

    import asyncio

    from app.solver.muteki.worker import official_worker as module

    monkeypatch.setattr(module, "_HEALTH_RETRY_DELAY_SECONDS", 0.0)

    class Profile:
        engine_id = "codex"
        driver_profile = {"protocol": "codex_cli", "engine": "codex"}
        environment: dict = {}

    worker = module.OfficialWorkerAdapter.__new__(module.OfficialWorkerAdapter)
    calls = {"n": 0}

    async def stub(self, profile, *, workspace):
        calls["n"] += 1
        return False, "WORKER_HEALTHCHECK_UNAUTHORIZED"

    monkeypatch.setattr(module.OfficialWorkerAdapter, "_health_once", stub)
    healthy, reason = asyncio.run(worker.health(Profile(), workspace="/tmp/ws"))

    assert healthy is False
    assert reason == "WORKER_HEALTHCHECK_UNAUTHORIZED"
    assert calls["n"] == 1, "a non-transient reason must fail fast"


def test_retry_notice_is_logged_at_a_level_this_deployment_survives(monkeypatch, caplog) -> None:
    """The retry must leave a trace at WARNING, not INFO.

    This deployment sets up no logging at all, so the root logger sits at
    WARNING.  Measured 2026-11-05 13:57 on a live Run: both ``probe failed``
    warnings were present in the log but the line explaining that a retry was
    happening was not, which makes a working retry indistinguishable from no
    retry at all -- exactly the question the log was being read to answer.
    """

    import asyncio
    import logging

    from app.solver.muteki.worker import official_worker as module

    monkeypatch.setattr(module, "_HEALTH_RETRY_DELAY_SECONDS", 0.0)

    class Profile:
        engine_id = "codex"
        driver_profile = {"protocol": "codex_cli", "engine": "codex"}
        environment: dict = {}

    worker = module.OfficialWorkerAdapter.__new__(module.OfficialWorkerAdapter)
    calls = {"n": 0}

    async def stub(self, profile, *, workspace):
        calls["n"] += 1
        return (False, "WORKER_HEALTHCHECK_TIMEOUT") if calls["n"] == 1 else (True, "")

    monkeypatch.setattr(module.OfficialWorkerAdapter, "_health_once", stub)

    with caplog.at_level(logging.WARNING):
        healthy, _ = asyncio.run(worker.health(Profile(), workspace="/tmp/ws"))

    assert healthy is True
    retry_records = [
        record
        for record in caplog.records
        if "retrying once" in record.getMessage()
    ]
    assert retry_records, "the retry must be announced"
    assert retry_records[0].levelno >= logging.WARNING, (
        "INFO is invisible here -- this deployment configures no logging"
    )


# --- intel:public-poc lane ------------------------------------------------


def test_intel_lane_exists_and_demands_chat_completions():
    """``lookup_public_poc`` is bound only on the chat-completions protocol.

    Measured over 484 Runs: zero calls, because every Run went to CLI Workers
    that run inside the container and cannot reach a host-side function.  Naming
    the tool in the CLI goal was an instruction that could never be obeyed, so
    the capability is reached through its own lane instead.
    """

    from app.solver.muteki.coordinator import _RACE_LANES

    intel = [lane for lane in _RACE_LANES if lane["lane_key"] == "intel:public-poc"]
    assert len(intel) == 1
    assert intel[0]["requires_protocol"] == "chat_completions"


def test_intel_lane_goal_names_the_tool_and_forbids_scanning():
    from app.solver.muteki.coordinator import _RACE_LANES

    lane = next(l for l in _RACE_LANES if l["lane_key"] == "intel:public-poc")
    goal = lane["goal_template"].format(label=lane["label"])
    assert "lookup_public_poc" in goal
    # It must read the board rather than re-derive the fingerprint.
    assert "read-facts" in goal
    # And it must not compete with the exploit lane for the target.
    assert "Do NOT attack the target" in goal


def test_every_lane_template_fits_the_intent_limit():
    """``intent_goal`` truncates at 600; a longer template loses its own tail."""

    from app.solver.muteki.coordinator import _RACE_LANES

    for lane in _RACE_LANES:
        template = lane.get("goal_template")
        if template:
            rendered = template.format(label=lane["label"])
            assert len(rendered) <= 600, f"{lane['lane_key']} is {len(rendered)} chars"


def test_cli_goal_no_longer_names_an_unreachable_tool():
    from app.solver.muteki.coordinator import _BACKFILL_GOAL_PREFIX, _SINGLE_WORKER_GOAL

    assert "lookup_public_poc" not in _SINGLE_WORKER_GOAL
    # The local corpus is the same information, in a readable form.
    assert "/home/kali/pocs" in _SINGLE_WORKER_GOAL
    # And the combined goal must still fit.
    combined = _BACKFILL_GOAL_PREFIX.format(missing=1) + _SINGLE_WORKER_GOAL
    assert len(combined) <= 600
