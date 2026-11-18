"""Merging extracted payloads must not silently disarm the matcher.

The first merge looked correct by every obvious measure -- entry count up,
runnable payloads up 32 -> 278 -- and broke two tests.  Both failures had the
same shape: a *new* entry took a slot that a *curated* entry was using, and
``MAX_ENTRIES = 2`` is small enough that one extra entry displaces something.

Concretely, adding ``vulhub:weblogic:cve-2018-2628`` next to the hand-written
``cve-2018-2628-weblogic-t3-negative`` pushed the negative entry out of the
two-slot budget.  That negative entry is what tells the Worker the JRMP chain
cannot call back; without it ``exploit_stop_loss_gate`` returned empty and the
Worker would burn a full round on an undeliverable exploit.

These tests pin the merge rules that prevent it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "merge_vulhub_playbook", ROOT / "scripts" / "merge_vulhub_playbook.py"
)
merge_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_mod)


def entry(id_: str, *, runnable: bool, cve: str | None = None, source: str = "vulhub",
          kind: str = "poc", failures: list[str] | None = None) -> dict:
    payload = (
        "POST /api/v1/hosts/command/search HTTP/1.1\n"
        "Host: {{host}}\n"
        "Content-Type: application/json\n"
        "Content-Length: 24\n\n"
        '{"page":1,"search":"x"}'
        if runnable
        else "POST /x"
    )
    return {
        "id": id_,
        "kind": kind,
        "title": f"{cve or 'x'} thing",
        "technique": "rce",
        "summary": "s",
        "preconditions": [],
        "target_stack": [cve] if cve else [],
        "payload_template": payload,
        "verification_method": [],
        "known_failures": failures if failures is not None else ["Imported mechanically"],
        "steps": [],
        "post_exploitation": [],
        "provenance": {"source_type": "reference-corpus", "source_ref": source},
        "trust_level": "unverified",
        "verified_solve": False,
        "published_at": None,
    }


# --- runnable detection ----------------------------------------------------


def test_endpoint_card_is_not_runnable():
    assert merge_mod.is_runnable(entry("a", runnable=False)) is False


def test_full_request_is_runnable():
    assert merge_mod.is_runnable(entry("a", runnable=True)) is True


# --- curated entries survive -------------------------------------------------


def test_curated_entry_is_not_replaced_by_an_extraction():
    """Same id: the curated semantics win, the payload may be borrowed."""

    old = [entry("vulhub:x:cve-2020-1", runnable=False, cve="CVE-2020-1",
                 failures=["★★★★ 实测：不需要反连"])]
    new = [entry("vulhub:x:cve-2020-1", runnable=True, cve="CVE-2020-1")]

    merged, stats = merge_mod.merge(old, new)
    assert stats["replaced"] == 0
    assert stats["enriched"] == 1
    got = merged[0]
    # The measurement survives...
    assert "不需要反连" in " ".join(got["known_failures"])
    # ...and it gained the request it lacked.
    assert "HTTP/1.1" in got["payload_template"]


def test_curated_entry_keeping_its_payload_loses_nothing():
    old = [entry("vulhub:x:cve-2020-2", runnable=True, cve="CVE-2020-2",
                 failures=["★★ 回显在响应体第 3 行"])]
    new = [entry("vulhub:x:cve-2020-2", runnable=True, cve="CVE-2020-2")]

    merged, stats = merge_mod.merge(old, new)
    assert stats["kept_old"] == 1
    assert "回显" in " ".join(merged[0]["known_failures"])


# --- the budget hazard ------------------------------------------------------


def test_same_cve_under_a_different_id_does_not_take_the_slot():
    """This is the regression the suite caught.

    Two entries for one CVE compete for ``MAX_ENTRIES = 2``.  If the extracted
    duplicate sorts first, the curated one is pushed off the list and its
    measurement (a negative result, usually) is lost.
    """

    old = [entry("cve-2018-2628-weblogic-t3-negative", runnable=False,
                 cve="CVE-2018-2628", kind="negative", source="vulhub")]
    new = [entry("vulhub:weblogic:cve-2018-2628", runnable=True, cve="CVE-2018-2628")]

    merged, stats = merge_mod.merge(old, new)
    ids = [e["id"] for e in merged]
    assert "vulhub:weblogic:cve-2018-2628" not in ids, (
        "the extraction must not sit next to a curated entry for the same CVE"
    )
    assert "cve-2018-2628-weblogic-t3-negative" in ids
    assert stats["cve_already_curated"] == 1


def test_hand_written_playbook_cves_are_excluded():
    """``cve-playbook.jsonl`` is a separate matcher input; never shadow it."""

    curated = merge_mod.curated_playbook_cves()
    assert isinstance(curated, set)
    # A CVE the curated playbook demonstrably covers.
    if curated:
        sample = sorted(curated)[0]
        old: list[dict] = []
        new = [entry(f"vulhub:x:{sample.lower()}", runnable=True, cve=sample)]
        merged, stats = merge_mod.merge(old, new)
        assert merged == []
        assert stats["cve_already_curated"] == 1


def test_a_runnable_entry_replaces_an_endpoint_card_with_the_same_id():
    old = [entry("vulhub:x:cve-2020-3", runnable=False, cve="CVE-2020-3")]
    new = [entry("vulhub:x:cve-2020-3", runnable=True, cve="CVE-2020-3")]

    merged, stats = merge_mod.merge(old, new)
    assert stats["replaced"] == 1
    assert "HTTP/1.1" in merged[0]["payload_template"]


def test_verified_solve_survives_a_replacement():
    old = [entry("vulhub:x:cve-2020-4", runnable=False, cve="CVE-2020-4")]
    old[0]["verified_solve"] = True
    old[0]["trust_level"] = "curated"
    new = [entry("vulhub:x:cve-2020-4", runnable=True, cve="CVE-2020-4")]

    merged, _ = merge_mod.merge(old, new)
    assert merged[0]["verified_solve"] is True
    assert merged[0]["trust_level"] == "curated"


def test_ids_stay_unique():
    old = [entry("a", runnable=True, cve="CVE-2020-5")]
    new = [entry("b", runnable=True, cve="CVE-2020-6"), entry("a", runnable=True, cve="CVE-2020-5")]
    merged, _ = merge_mod.merge(old, new)
    ids = [e["id"] for e in merged]
    assert len(ids) == len(set(ids))


def test_output_is_deterministically_ordered():
    old = [entry("z", runnable=True, cve="CVE-2020-7")]
    new = [entry("a", runnable=True, cve="CVE-2020-8")]
    merged, _ = merge_mod.merge(old, new)
    assert [e["id"] for e in merged] == sorted(e["id"] for e in merged)
