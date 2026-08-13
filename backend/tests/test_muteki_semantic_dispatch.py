from __future__ import annotations

from types import SimpleNamespace

from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.semantic_dispatch import (
    acquire_dispatch_semantics,
    release_dispatch_semantics,
)


def _challenge() -> SimpleNamespace:
    return SimpleNamespace(
        id="semantic-dispatch",
        name="Semantic dispatch",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )


def test_upstream_graph_reserves_and_releases_lane_and_resource(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "semantic.sqlite",
        challenge=_challenge(),
        challenge_id="run-semantic",
    )
    try:
        decision = acquire_dispatch_semantics(
            graph,
            worker_id="worker-1",
            intent_id="intent-1",
            payload={
                "lane_key": "destructive:tcp:443@target.test",
                "risk_class": "destructive",
                "resource_key": "target.test:session",
            },
        )
        assert decision.allowed is True
        assert decision.reservation is not None
        assert graph.active_lanes()
        assert graph.active_resource_locks()

        release_dispatch_semantics(graph, decision.reservation)

        assert graph.active_lanes() == []
        assert graph.active_resource_locks() == []
    finally:
        graph.close()


def test_suppressed_route_is_not_dispatched(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "suppressed.sqlite",
        challenge=_challenge(),
        challenge_id="run-suppressed",
    )
    try:
        graph.suppress_route(
            actor="review",
            route_hash="web:login:legacy",
            reason="verified dead end",
        )
        decision = acquire_dispatch_semantics(
            graph,
            worker_id="worker-1",
            intent_id="intent-1",
            payload={"route_hash": "web:login:legacy"},
        )
        assert decision.allowed is False
        assert decision.reason == "ROUTE_SUPPRESSED"
    finally:
        graph.close()


def test_same_route_has_one_active_owner(tmp_path) -> None:
    graph = UpstreamRuntimeGraph(
        tmp_path / "route-owner.sqlite",
        challenge=_challenge(),
        challenge_id="run-route-owner",
    )
    try:
        first = acquire_dispatch_semantics(
            graph,
            worker_id="worker-1",
            intent_id="intent-1",
            payload={"route_hash": "web:shared:route"},
        )
        second = acquire_dispatch_semantics(
            graph,
            worker_id="worker-2",
            intent_id="intent-2",
            payload={"route_hash": "web:shared:route"},
        )
        assert first.allowed is True
        assert second.allowed is False
        assert second.reason == "ROUTE_UNAVAILABLE"
        release_dispatch_semantics(graph, first.reservation)
        assert acquire_dispatch_semantics(
            graph,
            worker_id="worker-2",
            intent_id="intent-2",
            payload={"route_hash": "web:shared:route"},
        ).allowed is True
    finally:
        graph.close()


def test_compatibility_graph_without_semantic_capabilities_passes_through() -> None:
    graph = object()
    decision = acquire_dispatch_semantics(
        graph,
        worker_id="worker-1",
        intent_id="intent-1",
        payload={"route_hash": "web:generic:read"},
    )
    assert decision.allowed is True
    assert decision.reservation is None
