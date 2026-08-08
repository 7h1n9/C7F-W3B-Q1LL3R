from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.solver.muteki import EngineProfile, MutekiCoordinator, MutekiGraph, MutekiReason
from app.solver.muteki.core.stage_policy import StagePolicy
from app.solver.muteki.graph import Intent
from app.solver.muteki.worker import WorkerEngine, WorkerPool, WorkerResult
from app.solver.muteki.worker.poc import PocWorker
from app.solver.muteki.worker.review_worker import ReviewWorker
from app.solver.muteki.workers import MutekiWorkerPool


@dataclass
class FakeEngine(WorkerEngine):
    name: str
    available: bool = True

    async def execute(self, intent: Intent, workspace: str) -> WorkerResult:
        return WorkerResult(True, self.name, output=f"{self.name}:{intent.description}")

    def engine_type(self) -> str:
        return self.name

    def health_check(self) -> bool:
        return self.available


def test_heterogeneous_worker_pool_prefers_idle_engine() -> None:
    pool = WorkerPool({"codex": FakeEngine("codex"), "claude": FakeEngine("claude"), "cursor": FakeEngine("cursor")})

    async def scenario() -> None:
        first = await pool.acquire("codex")
        assert first is not None and first.engine_type() == "codex"
        assert pool.get_available_engine("codex").engine_type() == "claude"
        await pool.release(first)
        assert pool.get_available_engine("codex").engine_type() == "codex"

    asyncio.run(scenario())


def test_worker_pool_dispatches_intent_to_selected_engine(tmp_path) -> None:
    pool = WorkerPool({"codex": FakeEngine("codex")})
    intent = Intent("i1", "inspect target", "open", None, "", payload={})
    result = asyncio.run(pool.execute(intent, str(tmp_path), preferred="codex"))
    assert result.success is True
    assert result.engine_type == "codex"


def test_stage_policy_enforces_roles_and_transitions() -> None:
    policy = StagePolicy()
    assert policy.get_allowed_roles("race") == ("race",)
    assert policy.get_allowed_roles("coordinator") == ("bootstrap", "explore", "review")
    assert policy.can_transition("prepare", "race") is True
    assert policy.can_transition("race", "coordinator") is True
    assert policy.can_transition("prepare", "coordinator") is False
    assert policy.can_spawn("coordinator", "review") is True
    assert policy.can_spawn("race", "review") is False


def test_graph_resource_claim_is_exclusive_and_poc_queries_survive_rebuild(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    poc_id = graph.save_poc(actor="worker-a", poc_id="poc-1", content="IDOR reproduction")
    assert graph.get_poc(poc_id).content == "IDOR reproduction"
    assert graph.list_pocs(category="idor")[0].poc_id == poc_id
    assert graph.claim_resource(worker="worker-a", resource_id="exclusive:target") is True
    assert graph.claim_resource(worker="worker-b", resource_id="exclusive:target") is False
    assert graph.list_resources() == ["exclusive:target"]
    graph.rebuild_projections()
    assert graph.get_poc(poc_id) is not None
    assert graph.list_resources() == ["exclusive:target"]
    assert graph.release_resource(worker="worker-a", resource_id="exclusive:target") is True
    assert graph.list_resources() == []
    graph.close()


def test_poc_worker_loads_shared_poc_into_workspace(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    graph.save_poc(actor="worker-a", poc_id="poc-unsafe/name", content="GET /tickets?id=2")
    materialized = PocWorker(graph).materialize("poc-unsafe/name", str(tmp_path / "workspace"))
    assert materialized is not None
    assert "GET /tickets" in Path(materialized.path).read_text(encoding="utf-8")
    intent = Intent("i-poc", "EXPLOIT_WITH_POC", "open", None, "", payload={"poc_id": "poc-unsafe/name"})
    result = asyncio.run(PocWorker(graph).execute(intent, str(tmp_path / "workspace"), FakeEngine("codex")))
    assert result.success is True
    graph.close()


def test_reason_inherits_graph_poc_after_classification(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    graph.add_fact(actor="race", content='{"classification":"PATH_TRAVERSAL","confidence":90,"evidence_refs":["ev-1"]}', verified=True, evidence_refs=["ev-1"])
    graph.save_poc(actor="race", poc_id="poc-path", content="GET /download?file=../secret")
    result = asyncio.run(MutekiReason(metadata={}).reason(graph))
    assert result.intents[0].goal == "EXPLOIT_WITH_POC"
    assert result.intents[0].payload["poc_id"] == "poc-path"
    graph.close()


def test_review_worker_marks_unverified_facts_repeated_routes_and_branches(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    graph.add_fact(actor="worker", content="possible A or B", evidence_refs=[])
    first = graph.propose_intent(actor="worker", description="repeat route")
    second = graph.propose_intent(actor="worker", description="repeat route")
    for intent_id in (first, second):
        assert graph.claim_intent(worker="worker", intent_id=intent_id)
        assert graph.conclude_intent(actor="worker", intent_id=intent_id, result="NO_PROGRESS")
    result = ReviewWorker(graph).run()
    assert result.suspicious_fact_ids
    assert result.dead_end_ids
    assert len(result.branch_intent_ids) == 2
    graph.close()


def test_coordinator_can_schedule_review_without_changing_race_roles(tmp_path) -> None:
    jobs = []

    async def runner(job):
        jobs.append(job)

    async def scenario() -> None:
        graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
        pool = MutekiWorkerPool(graph, runner, max_workers=4)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(lambda _: [{"goal": "inspect endpoint", "payload": {"tool_name": "http_request"}}]),
            pool,
            [EngineProfile("codex"), EngineProfile("claude")],
            config={"review_interval": 1},
        )
        await coordinator.run(max_ticks=1)
        assert any(item.role == "race" for item in jobs)
        assert any(item.payload.get("role") == "review" for item in graph.events_since() if item.event_type == "worker_started")
        assert any(item.payload.get("role") == "review" for item in graph.events_since() if item.event_type == "worker_finished")
        assert all(item.environment.get("MUTEKI_WORKSPACE") for item in jobs)
        graph.close()

    asyncio.run(scenario())
