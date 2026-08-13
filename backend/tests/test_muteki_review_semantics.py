from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.solver.muteki.adapter.upstream_events import project_upstream_event
from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.coordinator import CoordinatorConfig, MutekiCoordinator
from app.solver.muteki.events import EventType
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.worker.review_worker import ReviewWorker
from app.solver.muteki.workers import EngineProfile, MutekiWorkerPool, WorkerJob, WorkerOutcome


def _challenge() -> SimpleNamespace:
    return SimpleNamespace(
        id="review-semantics",
        name="Review semantics",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )


def test_review_suppresses_repeated_route_in_official_graph(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "review.sqlite",
        challenge=_challenge(),
        challenge_id="run-review",
    )
    try:
        for index in range(3):
            intent_id = graph.propose_intent(
                actor="reason",
                intent_id=f"intent-{index}",
                description="inspect the same route",
                payload={"route_hash": "web:surface:route"},
            )
            worker = f"worker-{index}"
            assert graph.claim_intent(worker=worker, intent_id=intent_id)
            assert graph.conclude_intent(
                actor=worker,
                intent_id=intent_id,
                result="FAILED_NO_PROGRESS",
            )

        result = ReviewWorker(graph).run()

        assert result.notes
        assert not graph.is_route_suppressed("web:surface:route")
        assert any(event.event_type == EventType.REVIEW_PROPOSAL for event in graph.events_since())
        decisions = graph.apply_review_proposals()
        assert decisions[0]["decision"] == "accepted"
        assert graph.is_route_suppressed("web:surface:route")
        events = graph.events_since()
        assert any(event.event_type == EventType.REVIEW_PROPOSAL_DECISION for event in events)
        assert any(event.event_type == "review_finding" for event in events)
    finally:
        graph.close()


def test_review_defers_route_suppression_below_upstream_failure_threshold(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "review-deferred.sqlite",
        challenge=_challenge(),
        challenge_id="run-review-deferred",
    )
    try:
        for index in range(2):
            intent_id = graph.propose_intent(
                actor="reason",
                intent_id=f"intent-{index}",
                description="inspect the same route",
                payload={"route_hash": "web:surface:route"},
            )
            worker = f"worker-{index}"
            assert graph.claim_intent(worker=worker, intent_id=intent_id)
            assert graph.conclude_intent(
                actor=worker,
                intent_id=intent_id,
                result="FAILED_NO_PROGRESS",
            )

        ReviewWorker(graph).run()
        decisions = graph.apply_review_proposals()

        assert decisions[0]["decision"] == "deferred"
        assert graph.is_route_suppressed("web:surface:route") is False
    finally:
        graph.close()


def test_coordinator_applies_upstream_review_decision(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "review-coordinator.sqlite",
        challenge=_challenge(),
        challenge_id="run-review-coordinator",
    )
    try:
        for index in range(3):
            intent_id = graph.propose_intent(
                actor="reason",
                intent_id=f"intent-{index}",
                description="inspect the same route",
                payload={"route_hash": "web:surface:route"},
            )
            worker = f"worker-{index}"
            assert graph.claim_intent(worker=worker, intent_id=intent_id)
            assert graph.conclude_intent(
                actor=worker,
                intent_id=intent_id,
                result="FAILED_NO_PROGRESS",
            )

        async def runner(_job):
            return WorkerOutcome("worker", "COMPLETED")

        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("review")],
            config=CoordinatorConfig(race_enabled=False, interval_seconds=0),
        )
        job = WorkerJob(
            worker_id="reviewer",
            role="review",
            engine_id="review",
            graph_path=str(graph.db_path),
            challenge_id=graph.challenge_id,
        )

        outcome = asyncio.run(coordinator._review_runner(job))

        assert "review_accepted=1" in outcome.result
        assert graph.is_route_suppressed("web:surface:route")
    finally:
        graph.close()


def test_review_splits_alternative_fact_into_branch_intents(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "branches.sqlite",
        challenge=_challenge(),
        challenge_id="run-branches",
    )
    try:
        graph.add_fact(
            actor="race",
            content="the surface may be an IDOR or a path traversal",
            verified=False,
        )

        result = ReviewWorker(graph).run()

        assert len(result.branch_intent_ids) == 2
        snapshot = graph.snapshot()
        assert all(item["status"] == "open" for item in snapshot["intents"])
        assert any(event.event_type == "branch_split" for event in graph.events_since())
    finally:
        graph.close()


def test_review_branch_preserves_only_explicit_allowlisted_tool_identity(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "branch-tools.sqlite",
        challenge=_challenge(),
        challenge_id="run-branch-tools",
    )
    try:
        graph.add_fact(
            actor="race",
            content="try sql_boolean_compare or invoke_unknown_tool",
            verified=False,
        )

        ReviewWorker(graph).run()

        branches = {
            item.description: item.payload or {}
            for item in graph.intents()
            if item.payload and item.payload.get("review_branch")
        }
        assert any(payload.get("tool_name") == "sql_boolean_compare" for payload in branches.values())
        assert all(
            payload.get("tool_name") in {None, "sql_boolean_compare"}
            for payload in branches.values()
        )
        assert all(
            payload.get("tool_identity_source") in {None, "review_allowlist"}
            for payload in branches.values()
        )
    finally:
        graph.close()


def test_upstream_review_and_lifecycle_events_have_typed_projection() -> None:
    event = project_upstream_event(
        {
            "seq": 7,
            "ts": 1_700_000_000,
            "actor": "reviewer",
            "kind": "route_suppressed",
            "payload": {"route_hash": "route-1", "reason": "repeated"},
        },
        challenge_id="run-1",
    )

    assert event.event_type == EventType.ROUTE_SUPPRESSED
    assert event.payload["upstream_event_type"] == "route_suppressed"
    assert event.payload["payload"]["route_hash"] == "route-1"
