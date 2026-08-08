from __future__ import annotations

import asyncio
import json

import pytest

from app.solver.multi_worker import MultiWorkerCoordinator
from app.solver.reason import ReasonPlanner
from app.solver.shared_graph import FlagGate, SharedGraph, SolverEventBus
from app.solver.worker.pool import OneShotWorkerPool


def test_shared_graph_claims_and_gate_are_durable(tmp_path) -> None:
    events = SolverEventBus(tmp_path / "events.jsonl")
    graph = SharedGraph(tmp_path / "blackboard.db", worker_id="worker-a", run_id="run-1", event_bus=events)

    fact_id = graph.write_fact("GET /search returned 200", verified=True)
    poc_id = graph.write_poc("reproduction request reference")
    intent_id = graph.propose_intent("inspect the search parameter")
    assert fact_id.startswith("fact_")
    assert poc_id.startswith("poc_")
    assert graph.snapshot()["pocs"][0]["id"] == poc_id
    assert graph.claim_intent(intent_id, "worker-a") == "WON"
    assert graph.claim_intent(intent_id, "worker-b") == "LOST"
    assert graph.complete_intent(intent_id) is True
    assert graph.claim_resource("target:search") == "WON"
    assert graph.claim_resource("target:search", "worker-b") == "LOST"
    assert graph.release_resource("target:search") is True

    candidate_id = graph.write_flag("flag{placeholder}", worker_output="flag{placeholder}")
    accepted_id = graph.write_flag("flag{real-value}", worker_output="command output: flag{real-value}")
    flags = {item.id: item for item in graph.read_flags()}
    assert flags[candidate_id].verified_by_gate is False
    assert flags[accepted_id].verified_by_gate is True
    assert [item.event_type for item in events.replay(run_id="run-1")] == [
        "FACT_WRITTEN",
        "POC_WRITTEN",
        "INTENT_PROPOSED",
        "INTENT_CLAIMED",
        "INTENT_CLAIMED",
        "INTENT_DONE",
        "FLAG_CANDIDATE",
        "FLAG_FOUND",
    ]


def test_graph_requires_worker_identity_and_gate_requires_real_output(tmp_path) -> None:
    graph = SharedGraph(tmp_path / "blackboard.db")
    with pytest.raises(ValueError, match="source_worker_id"):
        graph.write_fact("unattributed")
    decision = FlagGate().verify("flag{real-value}", worker_output="not present")
    assert decision.accepted is False
    assert decision.reason_code == "FLAG_NOT_IN_WORKER_OUTPUT"


def test_event_bus_sequence_and_resume(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    first = SolverEventBus(path)
    first.emit("WORKER_STARTED", run_id="run-1", payload={"worker_id": "w1"})
    first.emit("WORKER_FINISHED", run_id="run-1", payload={"worker_id": "w1"})
    resumed = SolverEventBus(path)
    resumed.emit("RUN_FINISHED", run_id="run-1")
    replayed = list(resumed.replay(run_id="run-1", last_event_id=2))
    assert replayed[0].sequence == 3
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["id"] == 3
    with pytest.raises(ValueError, match="Sensitive event field"):
        resumed.emit("WORKER_STARTED", run_id="run-1", payload={"output": "secret"})


def test_reason_planner_bounds_output_and_avoids_deadends() -> None:
    async def provider(_snapshot):
        return ["dead path", "new path", "another path", "fourth", "fifth"]

    planner = ReasonPlanner(provider)
    result = asyncio.run(
        planner.plan(
            {
                "facts": [{"content": "observed"}],
                "deadends": [{"description": "dead path"}],
                "flags": [],
            }
        )
    )
    assert [item.description for item in result] == ["new path", "another path", "fourth", "fifth"]


def test_coordinator_bootstraps_and_replans_only_after_graph_change(tmp_path) -> None:
    jobs = []

    async def runner(job):
        jobs.append(job)

    async def scenario() -> None:
        bus = SolverEventBus(tmp_path / "events.jsonl")
        graph = SharedGraph(tmp_path / "blackboard.db", worker_id="coordinator", run_id="run-1", event_bus=bus)
        pool = OneShotWorkerPool(runner, max_workers=4, event_bus=bus, run_id="run-1")
        coordinator = MultiWorkerCoordinator(
            graph,
            ReasonPlanner(lambda _: ["inspect the endpoint"]),
            pool,
            bootstrap_workers=2,
            event_bus=bus,
            run_id="run-1",
        )
        assert await coordinator.tick() is True
        await pool.wait()
        assert len(jobs) == 2
        assert await coordinator.tick() is False
        graph.write_fact("endpoint observed", source_worker_id="worker-1")
        assert await coordinator.tick() is True
        await pool.wait()
        assert len(jobs) == 3

    asyncio.run(scenario())
