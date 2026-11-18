"""Corpus dating: the field the time-held-out generalisation experiment rests on.

The protocol scores a challenge against only the knowledge that already existed
when it was created, which requires a per-entry ``published_at``.  A corpus that
shares one placeholder date satisfies every schema check while making that split
meaningless, so the audit and the exclusion switch are what keep the experiment
honest rather than merely well-formed.
"""

from __future__ import annotations

import json

import pytest

from app.knowledge.audit import DOMINANT_DATE_SHARE, audit_published_at
from app.knowledge.config import KnowledgeConfig, load_knowledge_config
from app.knowledge.models import KnowledgeItem
from app.knowledge.retriever import KnowledgeRetriever


def _item(item_id: str, published: str | None) -> dict:
    return {
        "id": item_id,
        "kind": "technique",
        "title": f"Technique {item_id}",
        "technique": "SQL injection boolean oracle",
        "summary": "A technique used to pin the corpus dating audit.",
        "preconditions": ["Authorized testing only."],
        "target_stack": ["MySQL"],
        "verification_method": ["Confirm the differential response."],
        "known_failures": ["WAF normalization can rewrite the payload."],
        "steps": ["Capture a baseline response."],
        "provenance": {"source_type": "manual", "source_ref": f"ref-{item_id}"},
        "published_at": published,
        "trust_level": "curated",
    }


def _write_corpus(root, entries) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "corpus.jsonl").write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def test_audit_flags_a_shared_placeholder_date(tmp_path) -> None:
    root = tmp_path / "knowledge"
    _write_corpus(
        root,
        [
            _item("a", "2020-12-03"),
            _item("b", "2020-12-03"),
            _item("c", "2020-12-03"),
            _item("d", None),
            _item("e", "2019-04-01"),
        ],
    )

    report = audit_published_at(root)

    assert report["total"] == 5
    assert report["suspected_placeholder_dates"] == {"2020-12-03": ["a", "b", "c"]}
    assert report["suspected_placeholder_count"] == 3
    assert report["missing_published_at"] == ["d"]
    assert report["missing_count"] == 1
    assert report["time_split_usable"] is False


def test_audit_passes_a_genuinely_dated_corpus(tmp_path) -> None:
    root = tmp_path / "knowledge"
    _write_corpus(root, [_item("a", "2019-01-02"), _item("b", "2020-06-07")])

    report = audit_published_at(root)

    assert report["suspected_placeholder_dates"] == {}
    assert report["missing_published_at"] == []
    assert report["time_split_usable"] is True


def test_audit_reports_against_the_shipped_corpus() -> None:
    """The shipped corpus must stay traceable, and must keep admitting its limit.

    This replaces an earlier version that asserted the corpus *failed* its audit.
    ``2020-12-03`` turned out to be the real OWASP WSTG v4.2 release date shared
    by same-source entries, so the fault was in the audit's heuristic rather than
    in the data.  What the corpus genuinely cannot do is answer whether a
    technique predates a given challenge, and that has to stay visible.
    """

    report = audit_published_at()

    # A floor, not an equality: the curated corpus may only grow.  Pinning the
    # exact count turned "a hand-verified entry joined the corpus" into a test
    # failure, which is the opposite of what this audit is guarding against.
    assert report["total"] >= 31
    assert report["missing_count"] == 0
    assert report["suspected_placeholder_count"] == 0
    assert report["time_split_usable"] is True
    # One document's release date still dominates, so the caveat must be loud.
    # ``cases.jsonl`` added three more WSTG-derived entries, so the group grew
    # with the corpus instead of diluting it.  The share is derived rather than
    # pinned: it is just the group's size over the auditable total, and pinning
    # the literal turned "one reviewed entry joined the corpus" into a false
    # alarm about the dominant date.
    largest = report["largest_shared_date"]
    assert largest["published_at"] == "2020-12-03"
    assert largest["count"] == 21
    assert largest["source_document"] == "owasp wstg v4.2"
    assert largest["share"] == round(21 / report["total"], 6)
    assert largest["share"] > DOMINANT_DATE_SHARE
    assert report["caveat"]


def test_a_shared_date_backed_by_one_document_is_not_a_placeholder(tmp_path) -> None:
    """The distinction the previous heuristic could not make.

    Several test sections of one release are published on that release's date;
    calling that a placeholder is what would push an operator into inventing
    per-section dates, which this project forbids.
    """

    root = tmp_path / "knowledge"
    entries = []
    for index, item_id in enumerate(("a", "b", "c")):
        entry = _item(item_id, "2020-12-03")
        entry["provenance"]["source_ref"] = f"OWASP WSTG v4.2: Test {index}; CWE-1"
        entries.append(entry)
    _write_corpus(root, entries)

    report = audit_published_at(root)

    assert report["suspected_placeholder_dates"] == {}
    assert report["shared_single_source_dates"] == {"2020-12-03": ["a", "b", "c"]}
    assert report["suspected_placeholder_count"] == 0
    assert report["time_split_usable"] is True
    assert report["largest_shared_date"]["source_document"] == "owasp wstg v4.2"
    # 3/3 entries on one release date leaves nothing to hold out, so the caveat
    # has to fire even though nothing is wrong with the data.
    assert report["caveat"]


def test_a_shared_date_spanning_documents_is_still_suspected(tmp_path) -> None:
    root = tmp_path / "knowledge"
    entries = []
    for item_id, document in (("a", "Doc One"), ("b", "Doc Two"), ("c", "Doc Three")):
        entry = _item(item_id, "2020-12-03")
        entry["provenance"]["source_ref"] = f"{document}: section; CWE-1"
        entries.append(entry)
    _write_corpus(root, entries)

    report = audit_published_at(root)

    assert report["suspected_placeholder_dates"] == {"2020-12-03": ["a", "b", "c"]}
    assert report["shared_single_source_dates"] == {}
    assert report["time_split_usable"] is False
    assert report["largest_shared_date"] is None


def _model(item_id: str, published) -> KnowledgeItem:
    return KnowledgeItem.model_validate(_item(item_id, published))


def test_undated_entries_can_be_excluded_from_retrieval() -> None:
    items = [
        _model("dated", "2019-01-02"),
        _model("undated", None),
    ]
    query = "SQL injection boolean oracle differential response baseline"

    permissive = KnowledgeRetriever(
        items=items, config=KnowledgeConfig(mode="on", root=".", top_k=5)
    )
    strict = KnowledgeRetriever(
        items=items,
        config=KnowledgeConfig(
            mode="on", root=".", top_k=5, require_published_at=True
        ),
    )

    permissive_ids = {result.item.id for result in permissive.retrieve(query=query)}
    strict_ids = {result.item.id for result in strict.retrieve(query=query)}

    assert permissive_ids == {"dated", "undated"}
    assert strict_ids == {"dated"}


def test_config_rejects_a_non_boolean_published_at_requirement() -> None:
    with pytest.raises(ValueError):
        KnowledgeConfig(mode="on", root=".", top_k=3, require_published_at="yes")


def test_require_published_at_defaults_to_false_and_reads_the_env(monkeypatch) -> None:
    monkeypatch.delenv("C7F_KNOWLEDGE_REQUIRE_PUBLISHED_AT", raising=False)
    assert load_knowledge_config().require_published_at is False

    monkeypatch.setenv("C7F_KNOWLEDGE_REQUIRE_PUBLISHED_AT", "true")
    assert load_knowledge_config().require_published_at is True

    monkeypatch.setenv("C7F_KNOWLEDGE_REQUIRE_PUBLISHED_AT", "maybe")
    with pytest.raises(ValueError):
        load_knowledge_config()
