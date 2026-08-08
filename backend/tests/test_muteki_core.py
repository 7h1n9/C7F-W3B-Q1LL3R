from __future__ import annotations

import json
import subprocess
import sys

from app.solver.muteki import EventType, MutekiFlagGate, MutekiGraph


def test_muteki_graph_replays_append_only_events_and_materializes_facts(tmp_path) -> None:
    seen = []
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1", event_subscriber=seen.append)
    fact_id = graph.add_fact(actor="worker-a", content="GET / returned 200", verified=True, evidence_refs=["artifact-1"])
    intent_id = graph.propose_intent(actor="coordinator", description="inspect the search endpoint")
    assert graph.claim_intent(worker="worker-a", intent_id=intent_id) is True
    assert graph.claim_intent(worker="worker-b", intent_id=intent_id) is False
    assert graph.conclude_intent(actor="worker-a", intent_id=intent_id, result="done") is True
    assert graph.facts(verified_only=True)[0].fact_id == fact_id
    assert [event.event_type for event in graph.events_since()] == [
        EventType.FACT_ADDED,
        EventType.INTENT_PROPOSED,
        EventType.INTENT_CLAIMED,
        EventType.INTENT_CONCLUDED,
    ]
    assert [event.sequence for event in seen] == [1, 2, 3, 4]
    graph.close()


def test_muteki_gate_only_accepts_real_output(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1", gate=MutekiFlagGate())
    candidate = graph.write_flag(actor="worker-a", flag="flag{placeholder}", real_output="flag{placeholder}")
    accepted = graph.write_flag(actor="worker-a", flag="flag{real-value}", real_output="command output\nflag{real-value}\n")
    assert graph.flags()[0].flag_id == candidate
    assert graph.flags()[0].verified_by_gate is False
    assert graph.flags()[1].flag_id == accepted
    assert graph.flags()[1].verified_by_gate is True


def test_muteki_skill_cli_uses_environment_blackboard(tmp_path) -> None:
    db_path = tmp_path / "shared_graph.db"
    env = {
        "MUTEKI_BLACKBOARD_DB": str(db_path),
        "MUTEKI_CHALLENGE_ID": "challenge-1",
        "MUTEKI_WORKER_ID": "worker-cli",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard", "write-fact", "endpoint observed", "--verified"],
        cwd="backend",
        env={**__import__("os").environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == 1
    reader = MutekiGraph(db_path, challenge_id="challenge-1")
    assert reader.facts(verified_only=True)[0].content == "endpoint observed"
    reader.close()


def test_muteki_projections_can_be_rebuilt_from_event_log(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "shared_graph.db", challenge_id="challenge-1")
    graph.add_fact(actor="worker-a", content="observed", verified=True)
    graph.add_dead_end(actor="worker-a", description="old route")
    intent_id = graph.propose_intent(actor="coordinator", description="new route")
    graph.claim_intent(worker="worker-a", intent_id=intent_id)
    graph.write_flag(actor="worker-a", flag="flag{real}", real_output="flag{real}")
    with graph._lock:
        graph._db.executescript("DELETE FROM facts; DELETE FROM dead_ends; DELETE FROM intents; DELETE FROM flags;")
        graph._db.commit()
    graph.rebuild_projections()
    assert len(graph.facts()) == 1
    assert len(graph.dead_ends()) == 1
    assert graph.intents()[0].status == "claimed"
    assert graph.flags(verified_only=True)[0].flag_value == "flag{real}"
    graph.close()
