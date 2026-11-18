"""Tests for policy-candidate mining.

The load-bearing properties are negative ones: no test-split leakage, no
uncorroborated constraints, no candidate without provenance, no flag in the
output.
"""

from __future__ import annotations

from app.training.candidates import (
    build_candidate_payload,
    command_shape,
    mine_dead_ends,
    mine_techniques,
    technique_labels,
)


def transition(
    *,
    run_id: str,
    sequence: int,
    command: str,
    facts: int = 0,
    dead_ends: int = 0,
    accepted: bool = False,
    split: str = "train",
    tool: str = "command_execution",
) -> dict:
    return {
        "run_id": run_id,
        "challenge_id": "chal-1",
        "sequence": sequence,
        "split": split,
        "state": {},
        "action": {"tool": tool, "command": command},
        "observation": {"delta_facts": facts, "delta_dead_ends": dead_ends},
        "labels": {"accepted": accepted, "final_status": "COMPLETED_SOLVED"},
    }


# ---------------------------------------------------------------------------
# Shape collapsing
# ---------------------------------------------------------------------------


def test_shape_collapses_arguments_but_keeps_the_idea():
    a = command_shape("curl -s http://target/x?id=1")
    b = command_shape("curl -s http://target/x?id=2")
    assert a == b
    assert "curl" in a


def test_shape_redacts_flags_in_commands():
    shape = command_shape("echo 'flag{r34l_4nsw3r}' > /tmp/out")
    assert "r34l_4nsw3r" not in shape


# ---------------------------------------------------------------------------
# Dead ends
# ---------------------------------------------------------------------------


def test_dead_end_needs_corroboration_from_two_runs():
    single = [
        transition(run_id="r1", sequence=1, command="nmap -p- target", dead_ends=1),
    ]
    assert mine_dead_ends(single) == []

    corroborated = single + [
        transition(run_id="r2", sequence=1, command="nmap -p- target", dead_ends=1),
    ]
    candidates = mine_dead_ends(corroborated)
    assert len(candidates) == 1
    assert candidates[0]["runs_hit"] == 2
    assert candidates[0]["source_run_ids"] == ["r1", "r2"]


def test_shape_that_ever_produced_a_fact_is_not_a_dead_end():
    # "Did not work twice" is not "does not work" when it worked elsewhere.
    items = [
        transition(run_id="r1", sequence=1, command="gobuster dir -u http://t", dead_ends=1),
        transition(run_id="r2", sequence=1, command="gobuster dir -u http://t", dead_ends=1),
        transition(run_id="r3", sequence=1, command="gobuster dir -u http://t", facts=1),
    ]
    assert mine_dead_ends(items) == []


def test_dead_end_mining_ignores_non_train_splits():
    items = [
        transition(run_id="r1", sequence=1, command="dirb http://t", dead_ends=1, split="dev"),
        transition(run_id="r2", sequence=1, command="dirb http://t", dead_ends=1, split="test"),
    ]
    assert mine_dead_ends(items) == []


# ---------------------------------------------------------------------------
# Technique evidence
# ---------------------------------------------------------------------------


def test_technique_labels_returns_every_match():
    labels = technique_labels("gobuster dir -u http://t && curl 'http://t/?id=-1 union select 1'")
    assert "recon.dir" in labels
    assert "sqli.union" in labels


def test_technique_effective_needs_two_accepted_runs():
    same = "curl 'http://{h}/?id=-1 union select 1,flag,3'"
    single = [
        transition(run_id="r1", sequence=1, command=same.format(h="a"), facts=1, accepted=True),
    ]
    effective, dead = mine_techniques(single)
    assert effective == [] and dead == []

    corroborated = single + [
        transition(run_id="r2", sequence=1, command=same.format(h="b"), facts=1, accepted=True),
    ]
    effective, _ = mine_techniques(corroborated)
    assert len(effective) == 1
    assert effective[0]["technique"] == "sqli.union"
    assert effective[0]["runs_fact"] == 2
    assert effective[0]["source_run_ids"] == ["r1", "r2"]


def test_fact_gain_in_unsolved_run_does_not_promote_a_technique():
    # Only an accepted run counts as positive evidence.
    items = [
        transition(run_id="r1", sequence=1, command="curl 'http://a/?id=-1 union select 1'", facts=1, accepted=False),
        transition(run_id="r2", sequence=1, command="curl 'http://b/?id=-1 union select 1'", facts=1, accepted=False),
    ]
    effective, _ = mine_techniques(items)
    assert effective == []


def test_technique_that_only_hits_walls_is_reported_as_dead():
    items = [
        transition(run_id="r1", sequence=1, command="sqlmap -u http://a/?id=1", dead_ends=1),
        transition(run_id="r2", sequence=1, command="sqlmap -u http://b/?id=1", dead_ends=1),
    ]
    effective, dead = mine_techniques(items)
    assert effective == []
    assert len(dead) == 1
    assert dead[0]["technique"] == "sqli.union"
    assert dead[0]["runs_dead"] == 2


def test_technique_with_both_signals_is_not_declared_dead():
    items = [
        transition(run_id="r1", sequence=1, command="curl 'http://a/?id=-1 union select 1'", dead_ends=1),
        transition(run_id="r2", sequence=1, command="curl 'http://b/?id=-1 union select 1'", dead_ends=1),
        transition(run_id="r3", sequence=1, command="curl 'http://c/?id=-1 union select 1'", facts=1, accepted=True),
    ]
    effective, dead = mine_techniques(items)
    assert dead == []
    # One accepted run is below the corroboration bar, so it is not promoted
    # either -- but it is enough to stop the wall verdict.
    assert effective == []


def test_technique_with_a_fact_gain_in_an_unsolved_run_is_not_dead():
    # recon that found paths in a run that failed elsewhere is not a wall.
    items = [
        transition(run_id="r1", sequence=1, command="ffuf -w wl -u http://a/FUZZ", dead_ends=1),
        transition(run_id="r2", sequence=1, command="ffuf -w wl -u http://b/FUZZ", dead_ends=1),
        transition(run_id="r3", sequence=1, command="ffuf -w wl -u http://c/FUZZ", facts=1),
    ]
    effective, dead = mine_techniques(items)
    assert dead == []
    assert effective == []


def test_technique_mining_ignores_non_train_splits():
    items = [
        transition(run_id="r1", sequence=1, command="curl 'http://a/?id=-1 union select 1'", facts=1, accepted=True, split="test"),
        transition(run_id="r2", sequence=1, command="curl 'http://b/?id=-1 union select 1'", facts=1, accepted=True, split="dev"),
    ]
    effective, dead = mine_techniques(items)
    assert effective == [] and dead == []


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def test_payload_carries_provenance_and_stats():
    items = [
        transition(run_id="r1", sequence=0, command="gobuster dir -u http://a", accepted=True),
        transition(
            run_id="r1", sequence=1,
            command="curl 'http://a/?id=-1 union select 1,2,3'", facts=1, accepted=True,
        ),
        transition(run_id="r2", sequence=0, command="gobuster dir -u http://b", accepted=True),
        transition(
            run_id="r2", sequence=1,
            command="curl 'http://b/?id=-1 union select 1,2,3'", facts=1, accepted=True,
        ),
    ]
    payload = build_candidate_payload(items, policy_version="pol-test", source_file="x.jsonl")
    assert payload["policy_version"] == "pol-test"
    assert payload["split_used"] == "train"
    assert payload["stats"]["transitions_read"] == 4
    assert payload["stats"]["technique_effective_candidates"] >= 1
    for candidate in payload["techniques_effective"]:
        assert candidate["source_run_ids"]
        assert candidate["runs_fact"] >= 2
