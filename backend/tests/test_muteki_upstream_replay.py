from types import SimpleNamespace

import pytest

from app.solver.muteki.adapter.upstream_replay import (
    UpstreamGraphReplay,
    UpstreamReplayConfig,
    UpstreamReplayDisabled,
)
from app.solver.muteki.upstream_bridge import open_upstream_shared_graph


def _challenge() -> SimpleNamespace:
    return SimpleNamespace(
        id="replay-challenge",
        name="Replay target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )


def test_replay_is_disabled_by_default() -> None:
    with pytest.raises(UpstreamReplayDisabled):
        UpstreamGraphReplay().replay_events(db_path="missing.sqlite", challenge=_challenge())


def test_enabled_replay_reads_official_graph_without_writing(tmp_path) -> None:
    challenge = _challenge()
    path = tmp_path / "official.sqlite"
    graph = open_upstream_shared_graph(db_path=str(path), challenge=challenge)
    try:
        graph.add_evidence(
            actor="race",
            source="http_request",
            fact="HTTP endpoint /health returned 200",
            artifact_id="evidence-1",
            verified=True,
        )
    finally:
        graph.close()

    replayed = UpstreamGraphReplay(UpstreamReplayConfig(enabled=True)).replay_events(
        db_path=path,
        challenge=challenge,
    )

    assert len(replayed) == 1
    assert replayed[0].event_type == "fact_added"
    assert replayed[0].verified is True
    assert replayed[0].challenge_id == challenge.id
    assert replayed[0].payload["payload"]["evidence_refs"] == ["evidence-1"]


def test_replay_supports_incremental_sequence_cursor(tmp_path) -> None:
    challenge = _challenge()
    path = tmp_path / "cursor.sqlite"
    graph = open_upstream_shared_graph(db_path=str(path), challenge=challenge)
    try:
        first = graph.add_evidence(
            actor="race",
            source="http_request",
            fact="first",
            verified=False,
        )
        graph.add_evidence(
            actor="race",
            source="http_request",
            fact="second",
            verified=False,
        )
    finally:
        graph.close()

    replayed = UpstreamGraphReplay(UpstreamReplayConfig(enabled=True)).replay_events(
        db_path=path,
        challenge=challenge,
        after_sequence=first,
    )

    assert [event.payload["payload"]["fact"] for event in replayed] == ["second"]
