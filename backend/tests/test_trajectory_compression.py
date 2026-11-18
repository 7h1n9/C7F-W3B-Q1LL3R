"""Compression must shrink the trajectory without losing what it is for.

Two classes of test:

* synthetic ones pin the *contract* -- segmentation, artifact protection,
  placeholder round-trip, determinism -- against inputs small enough to reason
  about by hand;
* corpus ones run the five sampled runs end to end and assert the numbers the
  report quotes (ratio, artifact coverage, and the three follow-up questions).

The corpus tests skip when the export is absent so that a clean checkout still
runs green; they do not skip past a *failing* assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.training.trajectory import (
    CompressedTrajectory,
    RunTrajectory,
    TrajectoryEvent,
    _payload_slices,
    artifact_coverage,
    compress_trajectory,
    expand_placeholder,
    expand_text,
    extract_artifacts,
    load_run_trajectory,
)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "training"
WORKSPACES = ROOT / "data" / "workspaces"

#: Five runs with five distinct ``group_key`` values, sampled once and frozen
#: here so the reported numbers are reproducible.  ``test_sample_covers_five_
#: group_keys`` below fails if the export ever makes this set overlap.
SAMPLE_RUN_IDS = (
    "147ab7a8-f974-4c0e-9d64-5269fb00e301",  # challenge:, 526 events, unsolved
    "5cb19393-5159-4ad3-b1b7-dea3bb4d2361",  # cve:CVE-2019-3396
    "38299983-509c-4519-9dba-1effb02726b3",  # challenge:, 464 events, solved
    "de4e1da1-473b-43a8-b89c-b55600d7b8ce",  # cve:CVE-2017-7525
    "f425b23a-0094-4fa4-802a-f650b4af8fb6",  # cve:CVE-2012-1823
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row(
    index: int,
    command: str,
    result: str = "",
    *,
    delta_facts: int = 0,
    delta_dead_ends: int = 0,
    facts: int = 0,
    dead_ends: int = 0,
    run_id: str = "run-1",
    event_id: str | None = None,
) -> dict:
    """A transition row in the exported shape."""

    return {
        "run_id": run_id,
        "challenge_id": "challenge-1",
        "event_id": event_id or f"evt-{index:04d}",
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
            "final_status": "COMPLETED_UNSOLVED",
            "accepted": False,
            "fresh_reproduction_verified": False,
        },
        "split": "train",
        "group_key": "challenge:challenge-1",
    }


def _synthetic_run(count: int = 25) -> RunTrajectory:
    """A run whose every event carries one URL, one path, one payload and one
    status line -- enough artifact load that a naive "keep the first line"
    compressor would visibly lose things."""

    events = []
    for index in range(count):
        command = (
            f"/bin/bash -lc \"cd /home/kali/workspace && curl -sS -i -m 20 "
            f"'http://10.10.10.{index % 4 + 1}:8080/api/v1/item?id={index}' "
            f"--data 'cmd=cat /etc/passwd' 2>&1 | tee out{index}.log\""
        )
        result = (
            f"HTTP/1.1 200 OK\nServer: nginx\n"
            f"path=/var/www/html/item{index}.php\n"
            f"VERIFIED_FACT=item {index} is readable\n"
            f"body: {'x' * 400}\n"
            f"DEADEND=no auth bypass on item {index}\n"
            f"flag{{synthetic_{index:03d}_value}}"
        )
        events.append(
            _row(index, command, result, delta_facts=1 if index % 5 == 0 else 0)
        )
    trajectory = RunTrajectory(
        run_id="synthetic-run",
        events=tuple(_row_to_event(row) for row in events),
        final_status="COMPLETED_UNSOLVED",
    )
    return trajectory


def _row_to_event(row: dict) -> TrajectoryEvent:
    from app.training.trajectory import normalise_events

    return normalise_events([row])[0]


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_segments_partition_every_event_exactly_once():
    trajectory = _synthetic_run(25)
    compressed = compress_trajectory(trajectory, window=10)

    assert [segment.index for segment in compressed.segments] == [1, 2, 3]
    assert [len(segment.steps) for segment in compressed.segments] == [10, 10, 5]
    covered = [step.index for segment in compressed.segments for step in segment.steps]
    assert covered == list(range(25))


def test_window_must_be_positive():
    with pytest.raises(ValueError):
        compress_trajectory(_synthetic_run(3), window=0)


def test_empty_trajectory_compresses_to_header_only():
    compressed = compress_trajectory(RunTrajectory(run_id="empty", events=()))
    assert compressed.segments == ()
    assert compressed.compressed_chars > 0  # the header line
    assert compressed.ratio == 0.0


# ---------------------------------------------------------------------------
# Artifact protection
# ---------------------------------------------------------------------------


def test_every_artifact_class_survives_verbatim():
    compressed = compress_trajectory(_synthetic_run(12), window=10)
    text = compressed.text

    assert "http://10.10.10.1:8080/api/v1/item?id=0" in text
    assert "10.10.10.1:8080" in text
    assert "/etc/passwd" in text
    assert "HTTP/1.1 200 OK" in text
    assert "VERIFIED_FACT=item 0 is readable" in text
    assert "DEADEND=no auth bypass on item 0" in text
    assert "flag{synthetic_000_value}" in text


def test_artifact_scan_reports_zero_loss():
    compressed = compress_trajectory(_synthetic_run(40), window=10)
    present, missing = artifact_coverage(compressed)
    assert missing == []
    assert len(present) >= 40


def test_unique_artifacts_bound_the_ratio_but_stay_below_half():
    """Adversarial input: every event carries a *distinct* URL, path, flag,
    fact and dead-end.

    Zero-loss protection means those bytes are irreducible, so the ratio here
    cannot reach the corpus's 20% -- this test pins the honest worst case
    (<50%) and exists to stop anyone from "improving" the ratio by dropping
    artifacts.
    """

    trajectory = _synthetic_run(60)
    compressed = compress_trajectory(trajectory, window=10)
    assert compressed.ratio < 0.50, compressed.ratio
    assert len(compressed.artifacts) >= 240


def test_payload_slice_skips_the_shell_wrapper():
    slices = _payload_slices(
        """/bin/bash -lc "curl -sS 'http://x/index.php?id=1' --data 'cmd=cat /etc/passwd'" """
    )
    assert slices == ["cmd=cat /etc/passwd"]


def test_payload_slice_ignores_plain_quoting():
    assert _payload_slices("/bin/bash -lc 'ls -la /home/kali/workspace'") == []


def test_path_artifacts_ignore_package_listing_noise():
    artifacts = extract_artifacts("/usr/lib/x86_64-linux-gnu/perl/5.20.2/threads.pm and /var/www/html/x.php")
    assert "/var/www/html/x.php" in artifacts
    assert not any(a.startswith("/usr/lib") for a in artifacts)


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------


def test_placeholders_round_trip_to_the_original_text():
    trajectory = _synthetic_run(15)
    compressed = compress_trajectory(trajectory, window=10)

    expanded = compressed.expand_all()
    for event in trajectory.events:
        assert event.command in expanded or event.command.strip() in expanded
        if event.result.strip():
            assert event.result in expanded


def test_expand_placeholder_rejects_unknown_tokens():
    compressed = compress_trajectory(_synthetic_run(3))
    with pytest.raises(KeyError):
        expand_placeholder(compressed, "<<seg:9:cmd#999>>")


def test_expand_text_resolves_tokens_inside_a_question():
    trajectory = _synthetic_run(12)
    compressed = compress_trajectory(trajectory, window=10)
    token = next(iter(compressed.placeholders))
    expanded = expand_text(compressed, f"原文：{token}")
    assert token not in expanded
    assert compressed.placeholders[token] in expanded


def test_collapsed_repeat_keeps_its_own_observation():
    """Two identical commands with *different* long outputs: the second line
    collapses to ``≡#0`` but must still carry (and tokenise) its own result."""

    rows = [
        _row(0, "curl -sS '{{target_url}}/a'", "alpha\n" + "A" * 300),
        _row(1, "curl -sS '{{target_url}}/b'", "beta"),
        _row(2, "curl -sS '{{target_url}}/a'", "gamma\n" + "G" * 300),
        _row(3, "curl -sS '{{target_url}}/c'", "delta"),
    ]
    compressed = compress_trajectory(rows, window=10)

    # #2 repeats #0, and #3 is the run's final command (always rendered whole).
    assert "≡#0" in compressed.text
    expanded = compressed.expand_all()
    assert "alpha\n" + "A" * 300 in expanded
    assert "gamma\n" + "G" * 300 in expanded


def test_last_command_is_rendered_verbatim_not_clipped():
    trajectory = _synthetic_run(23)
    compressed = compress_trajectory(trajectory, window=10)
    last = trajectory.events[-1].command.strip()
    assert last in compressed.text


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_input_yields_identical_text():
    first = compress_trajectory(_synthetic_run(30), window=10)
    second = compress_trajectory(_synthetic_run(30), window=10)
    assert first.text == second.text
    assert first.placeholders == second.placeholders


def test_key_order_of_input_rows_does_not_change_output():
    rows = [_row(i, f"echo step-{i}", f"out {i} /var/www/{i}.php") for i in range(12)]
    shuffled = [dict(reversed(list(row.items()))) for row in rows]
    assert compress_trajectory(rows).text == compress_trajectory(shuffled).text


# ---------------------------------------------------------------------------
# Corpus: the five sampled runs
# ---------------------------------------------------------------------------


def _corpus_available() -> bool:
    return DATA.is_dir() and any(DATA.glob("transitions-*.jsonl"))


requires_corpus = pytest.mark.skipif(not _corpus_available(), reason="training export not present")


@pytest.fixture(scope="module")
def sample_trajectories() -> dict[str, RunTrajectory]:
    if not _corpus_available():
        return {}
    return {
        run_id: load_run_trajectory(
            run_id, data_dir=str(DATA), workspace_dir=str(WORKSPACES)
        )
        for run_id in SAMPLE_RUN_IDS
    }


@requires_corpus
def test_sample_covers_five_group_keys(sample_trajectories):
    keys = {trajectory.group_key for trajectory in sample_trajectories.values()}
    assert len(keys) == 5, keys


@requires_corpus
@pytest.mark.parametrize("run_id", SAMPLE_RUN_IDS)
def test_corpus_run_compresses_below_twenty_percent(run_id, sample_trajectories):
    compressed = compress_trajectory(sample_trajectories[run_id])
    assert compressed.ratio < 0.20, (run_id, round(compressed.ratio, 4))
    assert compressed.event_count >= 400


@requires_corpus
@pytest.mark.parametrize("run_id", SAMPLE_RUN_IDS)
def test_corpus_run_keeps_every_artifact(run_id, sample_trajectories):
    compressed = compress_trajectory(sample_trajectories[run_id])
    present, missing = artifact_coverage(compressed)
    assert missing == [], (run_id, missing[:3])
    assert len(present) >= 40


@requires_corpus
@pytest.mark.parametrize("run_id", SAMPLE_RUN_IDS)
def test_three_follow_up_questions_are_answerable(run_id, sample_trajectories):
    """The compression's whole purpose: answer follow-ups without the raw run.

    1. What was the last command, verbatim?
    2. Which target addresses / endpoints appear?
    3. Which injection payloads appear?
    """

    trajectory = sample_trajectories[run_id]
    compressed = compress_trajectory(trajectory)
    text = compressed.text

    # (1) last non-empty command
    last = next(e.command.strip() for e in reversed(trajectory.events) if e.command.strip())
    assert last in text

    # (2) every target address / endpoint, taken from the source events
    addresses = {
        artifact
        for event in trajectory.events
        for artifact in extract_artifacts(event.command + "\n" + event.result)
        if artifact.startswith("{{target_url}}") or artifact.startswith("http")
    }
    assert addresses, "sample run has no addresses; the question is untestable"
    missing_addresses = sorted(a for a in addresses if a not in text)
    assert missing_addresses == [], missing_addresses[:3]

    # (3) every payload string
    payloads = {
        payload for event in trajectory.events for payload in _payload_slices(event.command)
    }
    missing_payloads = sorted(p for p in payloads if p not in text)
    assert missing_payloads == [], missing_payloads[:3]


@requires_corpus
def test_backfill_extends_truncated_excerpts(sample_trajectories):
    run_id = SAMPLE_RUN_IDS[1]
    with_backfill = sample_trajectories[run_id]
    without = load_run_trajectory(
        run_id, data_dir=str(DATA), workspace_dir=str(WORKSPACES), backfill=False
    )

    assert with_backfill.backfilled_events > 0
    assert with_backfill.source_chars > without.source_chars

    # Backfill must not smuggle a *new* target hostname in.  (The dataset
    # excerpt already contains bare hostnames -- only URL origins were
    # redacted upstream -- so the assertion is "no new hosts", not "no hosts".)
    from app.training.trajectory import HOST_RE

    def hosts(trajectory):
        return {m.group(0).lower() for e in trajectory.events for m in HOST_RE.finditer(e.result)}

    baseline = hosts(without)
    assert hosts(with_backfill) <= baseline


@requires_corpus
def test_backfill_is_deterministic(sample_trajectories):
    run_id = SAMPLE_RUN_IDS[2]
    again = load_run_trajectory(
        run_id, data_dir=str(DATA), workspace_dir=str(WORKSPACES)
    )
    assert [e.result for e in again.events] == [
        e.result for e in sample_trajectories[run_id].events
    ]


def test_backfill_masks_bare_hostnames_and_digests():
    from app.training.trajectory import _mask_backfilled

    masked = _mask_backfilled(
        "* Host direct-ctf2.dasctf.com:28075 was resolved.\n"
        "digest 5f4dcc3b5aa765d61d8327deb882cf99 leaked"
    )
    assert "dasctf.com" not in masked
    assert "5f4dcc3b5aa765d61d8327deb882cf99" not in masked
    assert "{{target_host}}" in masked


# ---------------------------------------------------------------------------
# Compression report helper (used by scripts/inspect_trajectory.py too)
# ---------------------------------------------------------------------------


def test_report_helper_counts_match_rendered_text(sample_trajectories):
    if not sample_trajectories:
        pytest.skip("training export not present")
    for trajectory in sample_trajectories.values():
        compressed: CompressedTrajectory = compress_trajectory(trajectory)
        assert compressed.compressed_chars == len(compressed.text)
        assert compressed.source_chars == sum(
            len(e.command) + len(e.result) for e in trajectory.events
        )
        # json-serialisable: the CLI writes this straight to stdout
        json.dumps({"ratio": compressed.ratio, "chars": compressed.compressed_chars})
