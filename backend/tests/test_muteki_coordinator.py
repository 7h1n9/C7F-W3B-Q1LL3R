from __future__ import annotations

import asyncio

from app.solver.muteki import (
    EngineProfile,
    EventType,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
)


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
            assert graph.claim_intent(worker=job.worker_id, intent_id=job.intent_id)
            graph.add_fact(actor=job.worker_id, content="worker observed a bounded endpoint", verified=True)

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
