from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.solver.muteki.adapter.upstream_runtime_graph import create_runtime_graph
from app.solver.muteki.coordinator import MutekiCoordinator, _container_graph_path
from app.solver.muteki.events import EventType
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
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.concluded: list[tuple[str, str, str]] = []
        self._intents = [
            SimpleNamespace(intent_id="i-open", status="open"),
            SimpleNamespace(intent_id="i-claimed", status="claimed"),
            SimpleNamespace(intent_id="i-done", status="done"),
        ]

    def intents(self):
        return list(self._intents)

    def conclude_intent(self, *, actor: str, intent_id: str, result: str) -> None:
        self.concluded.append((actor, intent_id, result))

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


def _coordinator_with_fake_graph(stop_reason: str | None) -> MutekiCoordinator:
    coordinator = object.__new__(MutekiCoordinator)
    coordinator._finalized = False
    coordinator._stop_reason = stop_reason
    coordinator.graph = _FakeGraph()
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


@pytest.mark.asyncio
async def test_finalize_closes_active_intents_as_timed_out() -> None:
    coordinator = _coordinator_with_fake_graph("MUTEKI_RUN_TIMEOUT")

    await coordinator.finalize(reason="STOPPED")

    assert coordinator.graph.concluded == [
        ("coordinator", "i-open", "timed_out"),
        ("coordinator", "i-claimed", "timed_out"),
    ]
    directive = next(
        event
        for event in coordinator.graph.events
        if event["event_type"] == EventType.COORDINATOR_DIRECTIVE
    )
    assert directive["payload"]["directive"] == "finalize_active_intents"
    assert directive["payload"]["closed_intents"] == 2
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
        ("coordinator", "i-open", "cancelled"),
        ("coordinator", "i-claimed", "cancelled"),
    ]
    finished = next(
        event
        for event in coordinator.graph.events
        if event["event_type"] == EventType.RUN_FINISHED
    )
    assert finished["payload"]["intent_result"] == "cancelled"
