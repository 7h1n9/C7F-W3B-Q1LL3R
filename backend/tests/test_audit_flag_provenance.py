"""Provenance audit: a flag only counts if the event log shows it.

The first version of this audit scanned only ``muteki.worker_command``
tool_result excerpts and reported 48% of solved runs as suspicious.  That was a
false alarm on two counts, and both are pinned here so the mistake cannot come
back:

1. Flags are recorded in ``muteki.flag_found`` / ``muteki.fact_added``, not in
   tool-result excerpts.
2. ``result_excerpt`` is truncated with a ``…[+N chars]`` marker (19% of them),
   so a flag can legitimately be in the discarded tail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_flag_provenance import classify, pattern_mismatch  # noqa: E402


def run_row(**over):
    base = {
        "id": "run-1",
        "name": "Test Challenge",
        "target_url": "http://example/",
        "flag_pattern": r"flag\{[^}]+\}",
        "finished_at": "2026-11-05 10:00:00",
    }
    base.update(over)
    return base


def flag(value, state="VALID"):
    return {"candidate": value, "review_state": state, "verified": 1, "source_tool_call_id": None}


def test_flag_in_flag_found_event_is_platform_confirmed():
    outputs = [
        (10, "run.created", "{}"),
        (161, "muteki.flag_found", json.dumps({"payload": {"payload": {"flag": "flag{abc}"}}})),
    ]
    verdict = classify(run_row(), [flag("flag{abc}")], outputs)
    assert verdict["verdict"] == "PLATFORM_CONFIRMED"
    assert verdict["suspect"] is False
    assert verdict["evidence_event"] == "muteki.flag_found"


def test_flag_only_in_tool_result_is_observed_not_confirmed():
    outputs = [
        (20, "muteki.worker_command", json.dumps({"payload": {"kind": "tool_result",
                                                            "result_excerpt": "output: flag{abc}"}})),
    ]
    verdict = classify(run_row(), [flag("flag{abc}")], outputs)
    assert verdict["verdict"] == "SEEN_IN_OUTPUT"
    assert verdict["suspect"] is False


def test_flag_only_in_model_narrative_is_flagged_apart():
    # `muteki.fact_added` is text the model wrote.  It is not proof, but it is
    # also not the same as a flag that appears nowhere at all.
    outputs = [
        (30, "muteki.fact_added", json.dumps({"payload": {"fact": "found flag{abc}"}})),
    ]
    verdict = classify(run_row(), [flag("flag{abc}")], outputs)
    assert verdict["verdict"] == "SEEN_IN_NARRATIVE"
    assert verdict["suspect"] is False


def test_flag_absent_everywhere_is_the_only_suspect_case():
    outputs = [(10, "run.created", "{}"), (11, "muteki.worker_step", "no flags here")]
    verdict = classify(run_row(), [flag("flag{abc}")], outputs)
    assert verdict["verdict"] == "NOT_OBSERVED"
    assert verdict["suspect"] is True


def test_solved_run_without_any_flag_is_reported_separately():
    # A missing flag is a data problem, not evidence of fabrication, so it must
    # not be mixed into the suspect list.
    verdict = classify(run_row(), [], [])
    assert verdict["verdict"] == "NO_FLAG"
    assert verdict["suspect"] is False


def test_pattern_mismatch_is_detected_as_metadata_issue():
    row = run_row(flag_pattern=r"CTF2\{[^}]+\}")
    mismatch = pattern_mismatch(row, [flag("n1book{git_looks_s0_easyfun}", state="OPEN")])
    assert mismatch is not None
    assert mismatch["accepted_prefix"] == "n1book{...}"
    assert "candidate" not in mismatch, "the raw flag must never enter the report"


def test_matching_pattern_reports_no_mismatch():
    row = run_row(flag_pattern=r"CTF2\{[^}]+\}")
    assert pattern_mismatch(row, [flag("CTF2{abc123}")]) is None


def test_flag_value_body_is_never_written_to_the_report():
    row = run_row(flag_pattern=r"flag\{[^}]+\}")
    mismatch = pattern_mismatch(row, [flag("CTF2{secret-body-here}")])
    assert mismatch is not None
    serialized = json.dumps(mismatch)
    assert "secret-body-here" not in serialized
