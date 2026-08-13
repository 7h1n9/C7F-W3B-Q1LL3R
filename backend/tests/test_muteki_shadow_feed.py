import asyncio
from types import SimpleNamespace

from app.solver.muteki.adapter.event_bridge import EventBridge
from app.solver.muteki.adapter.shadow_feed import UpstreamShadowFeed
from app.solver.muteki.adapter.upstream_replay import (
    UpstreamGraphReplay,
    UpstreamReplayConfig,
)
from app.solver.muteki.upstream_bridge import open_upstream_shared_graph


class FakeEventService:
    def __init__(self) -> None:
        self.events = []

    async def append(self, session, run_id, event_type, payload):
        self.events.append((run_id, event_type, payload))
        return payload


def test_shadow_feed_uses_separate_event_namespace_and_preserves_sequence(tmp_path) -> None:
    challenge = SimpleNamespace(
        id="shadow-challenge",
        name="Shadow target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )
    path = tmp_path / "shadow.sqlite"
    graph = open_upstream_shared_graph(db_path=str(path), challenge=challenge)
    try:
        graph.add_evidence(
            actor="race",
            source="http_request",
            fact="status=200",
            verified=True,
        )
    finally:
        graph.close()

    service = FakeEventService()
    bridge = EventBridge(object(), service=service, run_id="run-1")
    count = asyncio.run(
        UpstreamShadowFeed(UpstreamGraphReplay(UpstreamReplayConfig(enabled=True))).publish(
            db_path=path,
            challenge=challenge,
            bridge=bridge,
        )
    )

    assert count == 1
    assert service.events[0][1] == "muteki.shadow.fact_added"
    assert service.events[0][2]["muteki_sequence"] == 1
    assert service.events[0][2]["payload"]["payload"]["fact"] == "status=200"


def test_event_bridge_deduplication_includes_event_namespace() -> None:
    service = FakeEventService()
    bridge = EventBridge(object(), service=service, run_id="run-1")
    from app.solver.muteki.events import EventEnvelope

    live = EventEnvelope(1, "now", "run-1", "worker", "fact_added", {})
    shadow = EventEnvelope(1, "now", "run-1", "worker", "shadow.fact_added", {})
    assert asyncio.run(bridge.bridge(live)) is not None
    assert asyncio.run(bridge.bridge(shadow)) is not None
    assert asyncio.run(bridge.bridge(live)) is None
    assert len(service.events) == 2
