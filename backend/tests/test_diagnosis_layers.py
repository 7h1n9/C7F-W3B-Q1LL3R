"""The four layers must be separable, citable and reproducible.

Each test builds a trajectory that exhibits exactly one failure shape.  That is
the only way to prove a classifier works: the corpus cannot tell you whether a
verdict is right, because nobody labelled it.

The last block runs the rules over ten real failed runs and checks the output
is *well formed* (valid layer, cited evidence, a counterfactual that is an
action, a score inside 0-100).  It deliberately does not assert which layer
each run lands in -- that would be fitting the test to the implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.training.diagnosis import (
    LAYERS,
    TIE_BREAK_ORDER,
    Diagnosis,
    diagnose_run,
    diagnose_runs,
    extract_features,
    layer_distribution,
    merge_retries,
    progress_score,
)
from app.training.trajectory import (
    RunTrajectory,
    load_run_trajectories,
    normalise_events,
)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "training"
WORKSPACES = ROOT / "data" / "workspaces"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _row(
    index: int,
    command: str,
    result: str = "",
    *,
    facts: int = 0,
    dead_ends: int = 0,
    delta_facts: int = 0,
    delta_dead_ends: int = 0,
    event_id: str | None = None,
    status: str = "COMPLETED_UNSOLVED",
) -> dict:
    return {
        "run_id": "synthetic",
        "challenge_id": "challenge",
        "event_id": event_id or f"e{index:03d}",
        "sequence": index,
        "state": {
            "facts": facts,
            "dead_ends": dead_ends,
            "recent_facts": [],
            "recent_commands": [],
            "commands_issued": index,
        },
        "action": {"tool": "shell", "command": command, "worker_id": "w1", "turn": 1, "call_id": ""},
        "observation": {
            "result_excerpt": result,
            "delta_facts": delta_facts,
            "delta_dead_ends": delta_dead_ends,
        },
        "labels": {
            "final_status": status,
            "accepted": False,
            "fresh_reproduction_verified": False,
        },
        "split": "train",
        "group_key": "challenge:challenge",
    }


def _trajectory(rows: list[dict], run_id: str = "synthetic") -> RunTrajectory:
    events = normalise_events(rows)
    return RunTrajectory(
        run_id=run_id,
        events=events,
        final_status="COMPLETED_UNSOLVED",
        group_key="challenge:challenge",
    )


def _interface_run() -> RunTrajectory:
    """Container image is missing tools the plan assumes."""

    rows = []
    for index in range(18):
        if index % 3 == 0:
            command = f"curl -sS -i -m 20 '{{{{target_url}}}}/admin?id={index}'"
            result = "ping: command not found"
        elif index % 3 == 1:
            command = f"zcat evidence/run{index}.gz | head -20"
            result = "zcat: command not found"
        else:
            command = f"curl -sS -m 20 '{{{{target_url}}}}/api/v1/item?id={index}'"
            result = "HTTP/1.1 200 OK\n{\"items\": []}"
        rows.append(_row(index, command, result, facts=2, dead_ends=1))
    return _trajectory(rows)


def _knowledge_run() -> RunTrajectory:
    """Same request re-issued, no variant, nothing learned."""

    rows = []
    for index in range(20):
        if index in {2, 5, 8, 11, 14, 17}:
            command = "curl -sS -m 20 '{{target_url}}/api/login' -d 'user=admin&pass=admin'"
            result = f"HTTP/1.1 401 Unauthorized {{'attempt':{index}}}"
        else:
            command = f"curl -sS -m 20 '{{target_url}}/api/v1/item?id={index}'"
            result = "HTTP/1.1 404 Not Found"
        dead = 4 if index >= 14 else 0
        rows.append(
            _row(index, command, result, facts=2, dead_ends=dead, delta_dead_ends=1 if index >= 14 else 0)
        )
    return _trajectory(rows)


def _perception_run() -> RunTrajectory:
    """400 commands, facts never land: the channel is silent end to end."""

    rows = []
    for index in range(25):
        command = f"curl -sS -m 20 '{{{{target_url}}}}/p{index}.php' | head -40"
        result = "" if index % 2 else f"line {index}"
        rows.append(_row(index, command, result))
    return _trajectory(rows)


def _strategy_run() -> RunTrajectory:
    """Ten commands of local warm-up before the target is even addressed."""

    rows = []
    for index in range(12):
        if index < 10:
            command = f"cat /home/kali/workspace/notes{index}.md 2>/dev/null | head -30"
            result = f"# notes {index}\ntodo: read AGENTS.md"
        else:
            command = "curl -sS -i -m 20 '{{target_url}}/'"
            result = "HTTP/1.1 200 OK"
        rows.append(_row(index, command, result, facts=0))
    return _trajectory(rows)


# ---------------------------------------------------------------------------
# One shape, one layer
# ---------------------------------------------------------------------------


def test_interface_failure_is_attributed_to_li():
    diagnosis = diagnose_run(_interface_run())

    assert diagnosis.layer == "LI"
    assert "command not found" in diagnosis.root_cause
    assert diagnosis.confidence > 0
    assert diagnosis.evidence, "LI verdict must carry evidence"


def test_knowledge_gap_is_attributed_to_ld():
    diagnosis = diagnose_run(_knowledge_run())

    assert diagnosis.layer == "LD"
    assert "重复" in diagnosis.root_cause
    assert any(item.kind == "ld:repeat" for item in diagnosis.evidence)


def test_silent_fact_channel_is_attributed_to_lp():
    diagnosis = diagnose_run(_perception_run())

    assert diagnosis.layer == "LP"
    assert "facts=0" in diagnosis.root_cause
    assert diagnosis.features["facts_total"] == 0


def test_long_warmup_is_attributed_to_ls():
    diagnosis = diagnose_run(_strategy_run())

    assert diagnosis.layer == "LS"
    assert "暖场" in diagnosis.root_cause
    assert diagnosis.features["target_first"] == 10


def test_interface_outranks_perception_when_both_fire():
    """A broken container makes an empty fact channel unsurprising, so the
    interface must win the tie-break instead of being reported as a symptom."""

    rows = [
        _row(i, f"curl -sS -m 20 '{{{{target_url}}}}/x{i}'", "curl: command not found")
        for i in range(22)
    ]
    diagnosis = diagnose_run(_trajectory(rows))
    assert diagnosis.layer == "LI"
    assert diagnosis.layer_scores["LP"] > 0


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder", [_interface_run, _knowledge_run, _perception_run, _strategy_run]
)
def test_root_cause_cites_a_real_event_id(builder):
    trajectory = builder()
    diagnosis = diagnose_run(trajectory)

    assert diagnosis.evidence
    cited = diagnosis.evidence[0].event_id
    known = {event.event_id for event in trajectory.events}
    assert cited in known
    assert cited in diagnosis.root_cause


@pytest.mark.parametrize(
    "builder", [_interface_run, _knowledge_run, _perception_run, _strategy_run]
)
def test_counterfactual_is_an_executable_action(builder):
    diagnosis = diagnose_run(builder())

    assert "`" in diagnosis.counterfactual, diagnosis.counterfactual
    assert any(
        verb in diagnosis.counterfactual
        for verb in ("curl", "ls -la", "command -v", "write-fact")
    ), diagnosis.counterfactual


@pytest.mark.parametrize(
    "builder", [_interface_run, _knowledge_run, _perception_run, _strategy_run]
)
def test_blocking_hypothesis_is_open_and_specific(builder):
    diagnosis = diagnose_run(builder())

    assert diagnosis.blocking_hypothesis.startswith("假设")
    assert len(diagnosis.blocking_hypothesis) > 20


@pytest.mark.parametrize(
    "builder", [_interface_run, _knowledge_run, _perception_run, _strategy_run]
)
def test_progress_score_stays_inside_bounds(builder):
    diagnosis = diagnose_run(builder())
    assert 0.0 <= diagnosis.progress_score <= 100.0


def test_progress_score_docstring_forbids_capability_reading():
    """The caveat is a deliverable, not a comment: the paper's 80 != 80%.

    If somebody deletes the warning while "cleaning up" the docstring, this
    test is what stops them.
    """

    doc = progress_score.__doc__ or ""
    for phrase in ("不得作为能力指标", "deadline", "reward hacking", "not a capability"):
        assert phrase in doc, phrase


def test_progress_score_is_monotone_in_facts():
    low = progress_score({"n": 40, "facts_total": 1, "target_ratio": 0.5, "empty_ratio": 0.3, "repeat_ratio": 0.1, "dead_total": 0})
    high = progress_score({"n": 40, "facts_total": 10, "target_ratio": 0.5, "empty_ratio": 0.3, "repeat_ratio": 0.1, "dead_total": 0})
    assert high > low


def test_diagnosis_is_deterministic_and_json_shaped():
    first = diagnose_run(_knowledge_run()).to_dict()
    second = diagnose_run(_knowledge_run()).to_dict()
    assert first == second
    assert set(first["layer_scores"]) == set(LAYERS)
    assert isinstance(first["progress_score"], float)


def test_empty_trajectory_does_not_crash():
    diagnosis = diagnose_run([])
    assert diagnosis.layer in LAYERS
    assert diagnosis.progress_score == 0.0
    assert diagnosis.confidence == 0.0


def test_layer_distribution_counts_every_layer():
    diagnoses = diagnose_runs([_interface_run(), _knowledge_run(), _perception_run()])
    counts = layer_distribution(diagnoses)
    assert set(counts) == set(LAYERS)
    assert sum(counts.values()) == 3
    assert counts["LI"] == 1


# ---------------------------------------------------------------------------
# Guards against the two false positives that shaped the rules
# ---------------------------------------------------------------------------


def test_target_side_errors_are_not_interface_failures():
    """A php payload reporting 'No such file' is a wrong guess about the
    victim, not a broken worker container."""

    rows = [
        _row(
            i,
            "curl -sS '{{target_url}}/?-d+allow_url_include=1+-d+auto_prepend_file=php://input' "
            "--data '<?php system(\"cat /flag\"); ?>'",
            "<br />\n<b>Warning</b>: cat: /flag: No such file or directory in <b>php://input</b>",
        )
        for i in range(15)
    ]
    diagnosis = diagnose_run(_trajectory(rows))
    assert diagnosis.layer != "LI"
    assert diagnosis.features["interface_total"] == 0


def test_started_completed_duplicate_rows_are_merged():
    """The export logs a call twice; counting both would inflate the repeat
    counter and put almost every run in the knowledge layer."""

    rows = [
        _row(0, "ls -la /home/kali/workspace", ""),
        _row(1, "ls -la /home/kali/workspace", "total 20\ndrwxr-xr-x 1 kali kali 4096 ."),
        _row(2, "curl -sS '{{target_url}}/'", ""),
        _row(3, "curl -sS '{{target_url}}/'", "HTTP/1.1 200 OK"),
    ]
    merged = merge_retries(normalise_events(rows))
    assert len(merged) == 2
    assert merged[0].result.startswith("total 20")
    features = extract_features(rows)
    assert features["raw_events"] == 4
    assert features["n"] == 2
    assert features["repeat_pairs"] == 0


def test_tie_break_order_pins_causality():
    assert TIE_BREAK_ORDER == ("LI", "LP", "LD", "LS")


# ---------------------------------------------------------------------------
# Corpus: ten real failed runs
# ---------------------------------------------------------------------------


def _corpus_available() -> bool:
    return DATA.is_dir() and any(DATA.glob("transitions-*.jsonl"))


requires_corpus = pytest.mark.skipif(not _corpus_available(), reason="training export not present")


FAILED_STATUSES = {"COMPLETED_UNSOLVED", "TIMEOUT", "FAILED_ENGINE"}


def _ten_failed_runs() -> list[str]:
    """Deterministic sample of ten failed runs with enough events to diagnose.

    Read from the run-level sidecar (238 KB) instead of re-parsing the 68 MB
    transition export; the ids are sorted so the sample cannot drift between
    runs.
    """

    runs_path = sorted(DATA.glob("runs-*.jsonl"))[-1]
    rows = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    failed = [
        row["run_id"]
        for row in rows
        if row.get("status") in FAILED_STATUSES and int(row.get("command_events") or 0) >= 10
    ]
    return sorted(failed)[:10]


@requires_corpus
def test_ten_failed_runs_produce_well_formed_diagnoses():
    ids = _ten_failed_runs()
    assert len(ids) == 10
    # Default loader settings (log backfill on): the report quotes these, so
    # the test must not diagnose a different trajectory than the report does.
    trajectories = load_run_trajectories(
        ids, data_dir=str(DATA), workspace_dir=str(WORKSPACES)
    )
    assert len(trajectories) == 10

    diagnoses: list[Diagnosis] = diagnose_runs(trajectories.values())
    assert len(diagnoses) == 10
    for diagnosis in diagnoses:
        assert diagnosis.layer in LAYERS
        assert 0.0 <= diagnosis.progress_score <= 100.0
        if diagnosis.confidence > 0:
            assert diagnosis.evidence, diagnosis.run_id
            assert diagnosis.evidence[0].event_id in diagnosis.root_cause
        else:
            assert diagnosis.layer == "LS"  # residual owner, flagged by confidence 0

    counts = layer_distribution(diagnoses)
    assert sum(counts.values()) == 10


@requires_corpus
def test_corpus_diagnosis_is_deterministic():
    ids = _ten_failed_runs()[:3]
    trajectories = load_run_trajectories(
        ids, data_dir=str(DATA), workspace_dir=str(WORKSPACES)
    )
    first = [diagnose_run(t).to_dict() for t in trajectories.values()]
    second = [diagnose_run(t).to_dict() for t in trajectories.values()]
    assert first == second
