import asyncio
from types import SimpleNamespace

from app.solver.muteki import CoordinatorConfig, MutekiCoordinator, MutekiPhase, MutekiWorkerPool
from app.solver.muteki.adapter.upstream_runtime_graph import (
    UpstreamRuntimeGraph,
    create_runtime_graph,
    runtime_graph_backend,
)
from app.solver.muteki.core.orchestrator import MutekiOrchestrator
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.workers import EngineProfile, WorkerOutcome


def _challenge() -> SimpleNamespace:
    return SimpleNamespace(
        id="runtime-graph-challenge",
        name="Runtime graph target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )


def test_graph_factory_keeps_current_backend_as_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APP_MUTEKI_GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("MUTEKI_GRAPH_BACKEND", raising=False)

    graph = create_runtime_graph(
        tmp_path / "current.sqlite",
        challenge=_challenge(),
        challenge_id="run-1",
    )
    try:
        assert isinstance(graph, MutekiGraph)
    finally:
        graph.close()


def test_production_runtime_can_select_upstream_as_its_default(monkeypatch) -> None:
    monkeypatch.delenv("APP_MUTEKI_GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("MUTEKI_GRAPH_BACKEND", raising=False)

    assert runtime_graph_backend(default="upstream") == "upstream"


def test_upstream_backend_supports_current_runtime_graph_surface(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_MUTEKI_GRAPH_BACKEND", "upstream")
    seen = []
    graph = create_runtime_graph(
        tmp_path / "official.sqlite",
        challenge=_challenge(),
        challenge_id="run-2",
        event_subscriber=seen.append,
    )
    assert isinstance(graph, UpstreamRuntimeGraph)
    try:
        graph.add_fact(
            actor="race",
            content="HTTP endpoint /health returned 200",
            verified=True,
            evidence_refs=["evidence-1"],
        )
        intent_id = graph.propose_intent(
            actor="reason",
            description="inspect endpoint",
            payload={"tool_name": "http_request"},
        )
        assert graph.claim_intent(worker="worker-1", intent_id=intent_id)
        assert graph.conclude_intent(actor="worker-1", intent_id=intent_id, result="explored")
        graph.write_flag(actor="worker-1", flag="flag{verified}", real_output="flag{verified}")

        snapshot = graph.snapshot()

        assert snapshot["facts"][0]["content"] == "HTTP endpoint /health returned 200"
        assert snapshot["facts"][0]["evidence_refs"] == ["evidence-1"]
        assert snapshot["intents"][0]["status"] == "done"
        assert snapshot["flags"][0]["flag_value"] == "flag{verified}"
        assert any(event.event_type == "fact_added" for event in seen)
        assert any(event.event_type == "intent_concluded" for event in seen)
    finally:
        graph.close()


def test_upstream_dispatchable_intents_reclaims_expired_claim(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "expired-lease.sqlite",
        challenge=_challenge(),
        challenge_id="run-expired-lease",
    )
    try:
        intent_id = graph.propose_intent(
            actor="reason",
            description="recover the abandoned route",
        )
        assert graph.claim_intent(
            worker="worker-1",
            intent_id=intent_id,
            lease_s=-1.0,
        )

        dispatchable = graph.dispatchable_intents()

        assert [item.intent_id for item in dispatchable] == [intent_id]
        assert dispatchable[0].status == "open"
        assert dispatchable[0].claimed_by is None
    finally:
        graph.close()


def test_upstream_dispatchable_intents_keeps_live_claim_hidden(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "live-lease.sqlite",
        challenge=_challenge(),
        challenge_id="run-live-lease",
    )
    try:
        intent_id = graph.propose_intent(
            actor="reason",
            description="keep the active route owned",
        )
        assert graph.claim_intent(
            worker="worker-1",
            intent_id=intent_id,
            lease_s=60.0,
        )

        assert graph.dispatchable_intents() == []
    finally:
        graph.close()


def test_coordinator_claim_lease_outlasts_worker_window(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "coordinator-lease.sqlite", challenge_id="lease")
    claimed: list[float] = []

    original_claim = graph.claim_intent

    def claim(*, worker: str, intent_id: str, lease_s: float = 300.0) -> bool:
        claimed.append(lease_s)
        return original_claim(worker=worker, intent_id=intent_id, lease_s=lease_s)

    graph.claim_intent = claim  # type: ignore[method-assign]
    graph.propose_intent(actor="reason", description="claim a bounded route")

    async def scenario() -> None:
        pool = MutekiWorkerPool(
            graph,
            lambda _job: None,
            max_workers=1,
        )
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex")],
            config=CoordinatorConfig(
                race_enabled=False,
                worker_timeout_seconds=12,
                worker_wait_grace_seconds=3,
            ),
        )
        coordinator.phase = MutekiPhase.COORDINATOR
        await coordinator._dispatch_open_intent()

    try:
        asyncio.run(scenario())
        assert claimed == [315.0]
    finally:
        graph.close()


def test_upstream_revision_observes_external_graph_writer(tmp_path) -> None:
    database = tmp_path / "external-revision.sqlite"
    first = UpstreamRuntimeGraph(
        database,
        challenge=_challenge(),
        challenge_id="run-external-revision",
    )
    second = UpstreamRuntimeGraph(
        database,
        challenge=_challenge(),
        challenge_id="run-external-revision",
    )
    try:
        before = first.revision()
        second.add_fact(
            actor="worker-1",
            content="verified upstream graph evidence",
            verified=True,
            evidence_refs=["evidence-external"],
        )

        assert first.revision() > before
        assert first.snapshot()["facts"]
    finally:
        second.close()
        first.close()


def test_current_coordinator_can_run_over_official_graph_facade(tmp_path) -> None:
    challenge = _challenge()
    graph = UpstreamRuntimeGraph(
        tmp_path / "coordinator.sqlite",
        challenge=challenge,
        challenge_id="run-3",
    )

    async def worker(job):
        graph.add_fact(
            actor=job.worker_id,
            content="verified upstream graph evidence",
            verified=True,
            evidence_refs=["evidence-2"],
        )
        graph.write_flag(
            actor=job.worker_id,
            flag="flag{coordinator}",
            real_output="flag{coordinator}",
        )
        return WorkerOutcome(job.worker_id, "COMPLETED", flag_found=True, result="ok")

    try:
        result = asyncio.run(
            MutekiOrchestrator(
                graph,
                MutekiReason(),
                worker_runner=worker,
                engines=[EngineProfile("gateway-runner")],
                max_workers=1,
            ).run(max_rounds=2)
        )

        assert result.status == "COMPLETED_SOLVED"
        assert graph.flags(verified_only=True)[0].flag_value == "flag{coordinator}"
    finally:
        graph.close()
