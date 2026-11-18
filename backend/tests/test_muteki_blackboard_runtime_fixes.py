from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.solver.muteki.adapter.upstream_runtime_graph import create_runtime_graph
from app.solver.muteki.coordinator import MutekiCoordinator, _container_graph_path
from app.solver.muteki.events import EventType
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.phases import MutekiPhase


def test_container_graph_path_maps_host_path_to_worker_workspace() -> None:
    host = (
        r"D:\desktop\毕业设计\C7F-W3B-Q1LL3R\data\workspaces\run-1"
        r"\muteki\graph\upstream_shared_graph.db"
    )
    assert _container_graph_path(host) == (
        "/home/kali/workspace/graph/upstream_shared_graph.db"
    )
    assert _container_graph_path(r"graph\upstream_shared_graph.db") == (
        "/home/kali/workspace/graph/upstream_shared_graph.db"
    )
    assert _container_graph_path("muteki/graph/shared_graph.db") == (
        "/home/kali/workspace/graph/shared_graph.db"
    )


@pytest.mark.parametrize("backend", ["current", "upstream"])
def test_runtime_graph_uses_delete_journal_mode_for_worker_bind_mount(
    tmp_path, backend: str
) -> None:
    challenge = SimpleNamespace(
        id="challenge-1",
        name="blackboard journal test",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test/",
        flag_pattern=r"flag\{[^}]+\}",
    )
    db_path = tmp_path / f"{backend}.db"
    graph = create_runtime_graph(
        db_path,
        challenge=challenge,
        challenge_id="run-1",
        backend=backend,
    )
    try:
        connection = sqlite3.connect(str(db_path))
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.close()
        assert str(mode).lower() == "delete"
    finally:
        graph.close()


class _FakeGraph:
    """Fake that mirrors ``MutekiGraph``'s real Intent state machine.

    The transitions are deliberately as strict as the durable implementation:
    ``claim_intent`` only accepts ``open`` and ``conclude_intent`` only accepts
    ``claimed``.  A permissive fake is what let "close the open intents" look
    correct while the real guarded UPDATE matched zero rows.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.concluded: list[tuple[str, str, str]] = []
        self.claimed: list[tuple[str, str]] = []
        self._intents = [
            SimpleNamespace(intent_id="i-open", status="open"),
            SimpleNamespace(intent_id="i-claimed", status="claimed"),
            SimpleNamespace(intent_id="i-done", status="done"),
        ]

    def intents(self):
        return list(self._intents)

    def claim_intent(self, *, worker: str, intent_id: str, lease_s: float = 300.0) -> bool:
        for item in self._intents:
            if item.intent_id == intent_id and item.status == "open":
                item.status = "claimed"
                self.claimed.append((worker, intent_id))
                return True
        return False

    def conclude_intent(self, *, actor: str, intent_id: str, result: str) -> bool:
        for item in self._intents:
            if item.intent_id == intent_id and item.status == "claimed":
                item.status = "done"
                self.concluded.append((actor, intent_id, result))
                return True
        return False

    def emit_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    def flags(self, verified_only: bool = False):
        return []

    def release_claims(self, actor: str = "") -> None:
        return None


class _FakePool:
    active_count = 0

    async def cancel_all(self) -> None:
        return None


def _coordinator_with_graph(graph, stop_reason: str | None) -> MutekiCoordinator:
    coordinator = object.__new__(MutekiCoordinator)
    coordinator._finalized = False
    coordinator._stop_reason = stop_reason
    coordinator.graph = graph
    coordinator.official_worker = None
    coordinator._health_retry_task = None
    coordinator._reason_health_retry_task = None
    coordinator.pool = _FakePool()
    coordinator._semantic_reservations = {}
    coordinator._active_route_hashes = set()
    coordinator.phase = MutekiPhase.COORDINATOR
    coordinator.stage_policy = SimpleNamespace(can_transition=lambda *_: True)
    coordinator._change_phase = lambda phase: setattr(coordinator, "phase", phase)
    return coordinator


def _coordinator_with_fake_graph(stop_reason: str | None) -> MutekiCoordinator:
    return _coordinator_with_graph(_FakeGraph(), stop_reason)


@pytest.mark.asyncio
async def test_finalize_closes_active_intents_as_timed_out() -> None:
    coordinator = _coordinator_with_fake_graph("MUTEKI_RUN_TIMEOUT")

    await coordinator.finalize(reason="STOPPED")

    assert coordinator.graph.concluded == [
        # Priority 1: a direction already mid-verification is retired first.
        ("coordinator", "i-claimed", "timed_out"),
        # Priority 2: the pending direction is claimed by the Coordinator and
        # only then concluded -- never dropped while still ``open``.
        ("coordinator", "i-open", "timed_out"),
    ]
    assert coordinator.graph.claimed == [("coordinator", "i-open")]
    directive = next(
        event
        for event in coordinator.graph.events
        if event["event_type"] == EventType.COORDINATOR_DIRECTIVE
    )
    assert directive["payload"]["directive"] == "finalize_active_intents"
    assert directive["payload"]["closed_intents"] == 2
    assert directive["payload"]["claimed_during_finalize"] == 1
    assert directive["payload"]["unclosed_intents"] == 0
    assert directive["payload"]["intent_result"] == "timed_out"
    finished = next(
        event
        for event in coordinator.graph.events
        if event["event_type"] == EventType.RUN_FINISHED
    )
    assert finished["payload"]["active_intents"] == 2
    assert finished["payload"]["closed_intents"] == 2
    assert finished["payload"]["intent_result"] == "timed_out"


@pytest.mark.asyncio
async def test_finalize_marks_active_intents_cancelled_on_normal_stop() -> None:
    coordinator = _coordinator_with_fake_graph(None)

    await coordinator.finalize(reason="SOLVED")

    assert coordinator.graph.concluded == [
        ("coordinator", "i-claimed", "cancelled"),
        ("coordinator", "i-open", "cancelled"),
    ]
    assert coordinator.graph.claimed == [("coordinator", "i-open")]
    finished = next(
        event
        for event in coordinator.graph.events
        if event["event_type"] == EventType.RUN_FINISHED
    )
    assert finished["payload"]["intent_result"] == "cancelled"
    assert finished["payload"]["unclosed_intents"] == 0


@pytest.mark.asyncio
async def test_finalize_closes_open_intents_on_the_real_graph(tmp_path) -> None:
    """A timed-out Run must leave no ``open`` Intent on the durable graph.

    ``MutekiGraph.conclude_intent`` guards its UPDATE with ``status='claimed'``
    (see ``graph.py``), so concluding an ``open`` Intent silently matches zero
    rows.  Driving the real state machine here keeps that contract from
    drifting back behind a permissive fake.
    """

    graph = MutekiGraph(
        tmp_path / "intent-closure.sqlite",
        challenge_id="challenge-intent-closure",
    )
    graph.propose_intent(actor="reason", intent_id="i-pending", description="pending direction")
    graph.propose_intent(actor="reason", intent_id="i-inflight", description="mid-verification")
    assert graph.claim_intent(worker="worker-1", intent_id="i-inflight") is True

    coordinator = _coordinator_with_graph(graph, "MUTEKI_RUN_TIMEOUT")
    await coordinator.finalize(reason="STOPPED")

    assert graph.intents(status="open") == []
    by_id = {item.intent_id: item for item in graph.intents()}
    assert by_id["i-pending"].status == "done"
    assert by_id["i-inflight"].status == "done"
    concluded = [
        item.payload
        for item in graph.events_since()
        if item.event_type == EventType.INTENT_CONCLUDED
    ]
    assert [item["intent_id"] for item in concluded] == ["i-inflight", "i-pending"]
    assert {item["result"] for item in concluded} == {"timed_out"}
