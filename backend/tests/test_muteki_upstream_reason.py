import asyncio
import json

from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.adapter.upstream_reason import parse_upstream_reason_reply


def test_official_reason_json_is_consumed_by_current_reason_gate(tmp_path) -> None:
    reply = json.dumps(
        {
            "verdict": "explore",
            "intents": [
                {
                    "id": "I1",
                    "goal": "inspect the authenticated ticket endpoint",
                    "worker_class": "code",
                    "route_hash": "web:idor:tickets",
                    "rationale": "The verified ticket listing exposes an object reference.",
                    "from": [1],
                }
            ],
        }
    )
    graph = MutekiGraph(tmp_path / "reason.sqlite", challenge_id="run-1")
    try:
        reason = MutekiReason(provider=lambda _snapshot: reply)
        result = asyncio.run(reason.reason(graph))

        assert len(result.intents) == 1
        assert result.intents[0].goal == "inspect the authenticated ticket endpoint"
        assert result.intents[0].payload["route_hash"] == "web:idor:tickets"
    finally:
        graph.close()


def test_official_reason_envelope_preserves_verdict_and_drift() -> None:
    reply = json.dumps(
        {
            "verdict": "course_correct",
            "goal_met": False,
            "drift": "The current route is exhausted; inspect the authenticated business surface.",
            "intents": [],
        }
    )

    parsed = parse_upstream_reason_reply(reply)

    assert isinstance(parsed, list)
    assert parsed.verdict == "course_correct"
    assert "authenticated business surface" in parsed.drift
    assert parsed.goal_met is False


def test_current_reason_keeps_official_course_correct_signal(tmp_path) -> None:
    reply = json.dumps(
        {
            "verdict": "course_correct",
            "goal_met": False,
            "drift": "Switch to a different evidence-backed direction.",
            "intents": [],
        }
    )
    graph = MutekiGraph(tmp_path / "reason-course-correct.sqlite", challenge_id="run-2")
    try:
        result = asyncio.run(MutekiReason(provider=lambda _snapshot: reply).reason(graph))

        assert result.intents == ()
        assert result.verdict == "course_correct"
        assert result.drift == "Switch to a different evidence-backed direction."
    finally:
        graph.close()
