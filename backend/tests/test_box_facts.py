"""Mined facts from a box's own past Runs.

Two properties matter more than the feature itself, because both were violated by
the first implementation:

1. **A fact is a lead, not a fact.** Previous Runs recorded things that were
   wrong, and the box may have been rebuilt.  The rendered block must say so.
2. **Mining must not become a garbage chute.** Of the 18,752 long facts these
   boxes hold, only 732 are reconnaissance -- 61% are ``REVIEW_NEEDED`` lines and
   28% are endpoint telemetry.  ``scripts/mine_box_facts.is_usable`` is the filter
   that keeps the block readable, so it is tested directly here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from app.services import known_exploit as ke  # noqa: E402
from mine_box_facts import (  # noqa: E402
    MAX_BOX_CHARS,
    MAX_FACT_CHARS,
    mine_one,
    is_usable,
)


def _ch(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description="点击启动靶机。")


# --- the miner's filter ----------------------------------------------------


def test_process_bookkeeping_is_not_reconnaissance() -> None:
    """61% of the corpus is this shape; it must never reach a prompt."""

    assert not is_usable(
        "REVIEW_NEEDED fact_id=56; reason=unverified_or_missing_evidence and more text here"
    )


def test_engine_failure_is_not_knowledge() -> None:
    """The Jaccard-1.00 false positive.

    Two Runs on 资产保修核验平台 produced byte-identical fact sets whose entire
    content was a 401 from the model provider.  Without this exclusion the miner
    would publish "the box returns 401" as a finding about the target.
    """

    assert not is_usable(
        '[codex] {"type":"turn.failed","error":{"message":"unexpected status 401 '
        'Unauthorized: Missing bearer or basic authentication in header"}}'
    )
    assert not is_usable(
        "The worker got 401 Unauthorized from the provider, so nothing was tested"
    )


def test_endpoint_telemetry_is_not_knowledge() -> None:
    assert not is_usable(
        '{"endpoint": "/help", "request_count": 4, "status_code": 200, "success": true}'
    )


def test_real_reconnaissance_passes() -> None:
    assert is_usable(
        "/api/warranty/check accepts JSON fields `asset_no` and `department`; "
        "a valid sample returns `matched:true`"
    )
    assert is_usable(
        "Username enumeration confirmed: `email=admin` returns a password error "
        "while any other value returns user-not-found"
    )


def test_short_labels_are_rejected() -> None:
    """A one-liner is redicovered in a single request; it is not worth prompt space."""

    assert not is_usable("Found a login page at /login")


# --- the per-box budget ----------------------------------------------------


def test_a_box_never_exceeds_its_char_budget() -> None:
    raw = [("run-1", f"endpoint {i} accepts a `field{i}` parameter: " + "x" * 200)
           for i in range(40)]
    bf = mine_one("box", raw)
    assert sum(len(f) for f in bf.facts) <= MAX_BOX_CHARS
    assert all(len(f) <= MAX_FACT_CHARS for f in bf.facts)


def test_duplicate_observations_collapse() -> None:
    """Same fact recorded by two engines is one fact.

    The engine tag (``[codex]`` / ``[claude]``) is part of the record, not the
    observation: leaving it in gives the two copies different dedupe keys, so
    this also guards the normalise-before-filter order in ``mine_one``.
    """

    body = (
        "endpoint /api/export accepts a `scope` parameter and returns a download "
        "URL for the resulting archive when the request body is valid JSON"
    )
    raw = [("run-1", f"[codex] {body}"), ("run-2", f"[claude] {body}")]
    bf = mine_one("box", raw)
    assert len(bf.facts) == 1
    assert not bf.facts[0].startswith("[codex]")


# --- the rendered block ----------------------------------------------------


def test_rendered_block_is_labelled_as_unverified_leads() -> None:
    """The wording is the safety property, not decoration."""

    block = ke.box_facts_for(_ch("NSB_Login"))
    if not block:
        pytest.skip("box-facts.jsonl not generated in this checkout")
    assert "leads, not verified facts" in block
    assert "Re-confirm" in block


def test_an_unknown_box_renders_nothing() -> None:
    assert ke.box_facts_for(_ch("This Box Does Not Exist")) == ""


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """A Run must never fail because derived data was not generated."""

    rows = ke._load_box_facts(tmp_path / "nope.jsonl")
    assert rows == []


def test_a_malformed_line_does_not_drop_the_file(tmp_path: Path) -> None:
    path = tmp_path / "box-facts.jsonl"
    path.write_text(
        '{"box_name": "A", "facts": ["endpoint /x accepts a field"]}\n'
        "{ this is not json\n"
        '{"box_name": "B", "facts": ["endpoint /y returns data"]}\n',
        encoding="utf-8",
    )
    rows = ke._load_box_facts(path)
    assert [r["box_name"] for r in rows] == ["A", "B"]
