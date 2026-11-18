"""Evidence-based reordering must be safe, bounded and reversible.

The load-bearing properties: evidence only reorders (never adds), it needs real
corroboration, it cannot be fooled by a small sample, and a missing or broken
evidence file silently degrades to the old corpus-size ordering.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.training.technique_evidence import (
    EVIDENCE_TO_CANDIDATE,
    MIN_EVIDENCE_RATE,
    MIN_EVIDENCE_RUNS,
    apply_evidence_boost,
    candidate_evidence,
    load_evidence,
    write_evidence,
)

# The real corpus ranking measured on 2026-11-04 (9 eligible classes, 3 slots).
CORPUS_RANKING = [
    (25, "Server-side request forgery"),
    (16, "File upload bypass"),
    (13, "SQL injection"),
    (10, "PHP deserialization"),
    (9, "Command injection and RCE"),
    (4, "PHP source review"),
    (3, "Server-side template injection"),
    (1, "Local file inclusion and PHP wrappers"),
    (1, "JWT forgery"),
]


def names(items):
    return [name for _, name in items]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_missing_file_means_no_evidence(tmp_path: Path):
    assert load_evidence(tmp_path / "nope.json") == {}


def test_corrupt_file_means_no_evidence(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_evidence(path) == {}


def test_evidence_with_zero_runs_is_ignored(tmp_path: Path):
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"techniques": {"rce.ognl": {"runs_fact": 0, "runs_dead": 0}}}))
    assert load_evidence(path) == {}


def test_write_then_load_roundtrip(tmp_path: Path):
    path = write_evidence({"rce.ognl": {"runs_fact": 10, "runs_dead": 6}}, tmp_path / "e.json")
    loaded = load_evidence(path)
    assert loaded["rce.ognl"]["runs_fact"] == 10
    assert loaded["rce.ognl"]["rate"] == 10 / 16


# ---------------------------------------------------------------------------
# Merging labels onto candidate names
# ---------------------------------------------------------------------------


def test_three_sqli_labels_sum_into_one_candidate():
    merged = candidate_evidence(
        {
            "sqli.union": {"runs_fact": 2, "runs_dead": 3},
            "sqli.boolean": {"runs_fact": 2, "runs_dead": 4},
            "sqli.time": {"runs_fact": 0, "runs_dead": 1},
        }
    )
    assert merged["SQL injection"]["runs_fact"] == 4
    assert merged["SQL injection"]["runs_dead"] == 8
    assert merged["SQL injection"]["runs"] == 12


def test_merging_is_idempotent():
    # Re-merging an already-mapped mapping must not empty it: an earlier version
    # did exactly that, which made the whole ranking a silent no-op.
    once = candidate_evidence({"rce.ognl": {"runs_fact": 10, "runs_dead": 6}})
    twice = candidate_evidence(once)
    assert twice["Command injection and RCE"]["runs_fact"] == 10


def test_two_lfi_labels_sum_into_one_candidate():
    merged = candidate_evidence(
        {
            "lfi.traversal": {"runs_fact": 5, "runs_dead": 9},
            "lfi.wrapper": {"runs_fact": 3, "runs_dead": 6},
        }
    )
    assert merged["Local file inclusion and PHP wrappers"]["runs_fact"] == 8


def test_every_evidence_label_maps_to_a_known_candidate_shape():
    # A typo in the mapping silently drops evidence, so the keys are pinned.
    assert set(EVIDENCE_TO_CANDIDATE) >= {"rce.ognl", "lfi.traversal", "sqli.union"}
    for targets in EVIDENCE_TO_CANDIDATE.values():
        assert isinstance(targets, tuple) and targets


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------


def test_no_evidence_keeps_corpus_order():
    assert apply_evidence_boost(CORPUS_RANKING, {}) == CORPUS_RANKING


def test_strong_evidence_promotes_rce_into_the_three_slots():
    # The concrete loss this exists to fix: RCE is 5th by corpus size and never
    # survives a 3-slot cut, despite the best measured hit rate.
    evidence = candidate_evidence({"rce.ognl": {"runs_fact": 10, "runs_dead": 7}})
    assert names(apply_evidence_boost(CORPUS_RANKING, evidence))[:3][0] == "Command injection and RCE"


def test_lfi_is_lifted_from_last_despite_one_corpus_entry():
    evidence = candidate_evidence(
        {
            "lfi.traversal": {"runs_fact": 5, "runs_dead": 9},
            "lfi.wrapper": {"runs_fact": 3, "runs_dead": 6},
        }
    )
    ordered = names(apply_evidence_boost(CORPUS_RANKING, evidence))
    assert ordered.index("Local file inclusion and PHP wrappers") < 3


def test_thin_sample_does_not_promote():
    # Two runs is below MIN_EVIDENCE_RUNS; a 2/2 streak must not win.
    evidence = candidate_evidence({"ssti.eval": {"runs_fact": 2, "runs_dead": 0}})
    ordered = names(apply_evidence_boost(CORPUS_RANKING, evidence))
    assert ordered == names(CORPUS_RANKING)


def test_enough_runs_but_low_rate_does_not_promote():
    # 10 runs, 1 fact: plenty of evidence, but it says the technique fails.
    # It keeps its corpus position (2nd) rather than being promoted to 1st, and
    # it must not be pushed below classes it already outranks.
    evidence = candidate_evidence({"upload.bypass": {"runs_fact": 1, "runs_dead": 9}})
    ordered = names(apply_evidence_boost(CORPUS_RANKING, evidence))
    assert ordered.index("File upload bypass") == 1
    assert ordered[0] == "Server-side request forgery"


def test_promoted_group_is_ordered_by_rate():
    evidence = candidate_evidence(
        {
            "rce.ognl": {"runs_fact": 10, "runs_dead": 7},      # 59%
            "sqli.union": {"runs_fact": 4, "runs_dead": 8},     # 33%
        }
    )
    ordered = names(apply_evidence_boost(CORPUS_RANKING, evidence))
    assert ordered[0] == "Command injection and RCE"
    assert ordered.index("Command injection and RCE") < ordered.index("SQL injection")


def test_reordering_never_adds_or_drops_a_class():
    evidence = candidate_evidence({"rce.ognl": {"runs_fact": 10, "runs_dead": 7}})
    ordered = apply_evidence_boost(CORPUS_RANKING, evidence)
    assert sorted(names(ordered)) == sorted(names(CORPUS_RANKING))
    assert len(ordered) == len(CORPUS_RANKING)


def test_counts_survive_reordering():
    # The caller formats the corpus count into the probe line; a lost count
    # would change the text the Worker reads.
    evidence = candidate_evidence({"rce.ognl": {"runs_fact": 10, "runs_dead": 7}})
    ordered = apply_evidence_boost(CORPUS_RANKING, evidence)
    assert dict(ordered) == dict(CORPUS_RANKING)


def test_empty_ranking_is_safe():
    assert apply_evidence_boost([], {"x": {"runs_fact": 9, "runs_dead": 0}}) == []


def test_thresholds_are_sane():
    assert MIN_EVIDENCE_RUNS >= 2
    assert 0 < MIN_EVIDENCE_RATE <= 1
