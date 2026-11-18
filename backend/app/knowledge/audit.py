"""Audit the curated corpus for fields the evaluation protocol depends on.

The generalisation protocol requires every entry to carry a verifiable
publication date, so a challenge can be evaluated against only the knowledge
that already existed when it was created
(``knowledge_item.published_at < challenge.start_at - buffer``).

A corpus where most entries share one date looks like it makes that split
meaningless — but "shared date" and "placeholder date" are not the same thing.
Nineteen entries here cite the same OWASP WSTG v4.2 release, and that release
genuinely has one publication date; flagging them would be a false alarm that
pushes an operator towards inventing per-test dates, which is the one fix this
project has explicitly forbidden.

So the suspicion rule keys on *attribution*, not on the date itself: a shared
date is only a placeholder if the entries claiming it cannot be traced back to
one document.  The residual limitation — that a corpus dominated by one release
can only separate that release's era from everything else — is reported
separately as a caveat rather than as a failure, because it is a property of the
evidence, not a defect in the data.

Entries published from this project's own reviewed runs
(``provenance.source_type == "learned"``, see ``LearnedSkillService.publish``)
are excluded from the suspicion rule entirely.  Their date is the completion
date of the run that produced the knowledge rather than the release date of a
cited document, so a batch of them landing on one day is expected and says
nothing about placeholders.  They are still reported, and still counted in the
corpus total and in ``missing_published_at``.

Run it as ``python -m app.knowledge.audit``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.knowledge.loader import load_knowledge_items

# A handful of entries legitimately published on the same day is normal; a whole
# corpus on one date needs explaining.
DEFAULT_PLACEHOLDER_GROUP = 3
# Above this share of the corpus, a single document's release date leaves too
# little elsewhere for a time-held-out split to have anything to hold out.
DOMINANT_DATE_SHARE = 0.5
# Entries this project produced itself, dated by the run that produced them.
LEARNED_SOURCE_TYPE = "learned"
# Entries converted from an external reference collection.  They carry no
# publication date because the source is a living document with no per-section
# release date, and inventing one would defeat the audit's whole purpose.  They
# are retrieval material, not evidence for a time-held-out split, so they are
# counted separately instead of being reported as "missing a date" -- otherwise
# importing a reference corpus would flip ``time_split_usable`` to False and
# silently disable the very experiment the audit exists to gate.
REFERENCE_SOURCE_TYPE = "reference-corpus"


def _source_document(source_ref: str) -> str:
    """The document a citation points at, ignoring the section within it.

    ``OWASP WSTG v4.2: Testing for SQL Injection`` and ``…: Testing for LDAP
    Injection`` are different citations of one document, and it is that
    document's release date they share.
    """

    head = str(source_ref or "").strip()
    return head.split(":", 1)[0].strip().lower() if head else ""


def _is_learned(item: Any) -> bool:
    """True for entries this project produced from its own reviewed runs."""

    return str(item.provenance.source_type or "").strip().lower() == LEARNED_SOURCE_TYPE


def _date_counts(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item.published_at.isoformat() if item.published_at else ""] += 1
    return dict(sorted(counts.items()))


def audit_published_at(
    root: str | Path | None = None,
    *,
    min_group: int = DEFAULT_PLACEHOLDER_GROUP,
) -> dict[str, Any]:
    """Report entries whose ``published_at`` cannot support a time split."""

    if root is None:
        from app.knowledge.config import load_knowledge_config

        root = load_knowledge_config().root

    items = load_knowledge_items(root)
    # Reference-corpus items are retrieved but never used to justify a
    # time-held-out claim, so they stay out of the date audit entirely.
    auditable = [
        item
        for item in items
        if str(item.provenance.source_type or "").strip().lower() != REFERENCE_SOURCE_TYPE
    ]
    by_date: dict[str, list[Any]] = defaultdict(list)
    for item in auditable:
        by_date[item.published_at.isoformat() if item.published_at else ""].append(item)

    missing = sorted(item.id for item in by_date.pop("", []))

    # The suspicion heuristic only applies to entries whose date is a cited
    # document's release date.  A learned entry is dated by the run that produced
    # it, so it is grouped separately and can never be reported as a placeholder.
    learned = [item for item in items if _is_learned(item)]
    curated_by_date: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        if _is_learned(item) or item.published_at is None:
            continue
        curated_by_date[item.published_at.isoformat()].append(item)

    # A shared date backed by one document is a version date; backed by several,
    # or by no attribution at all, it is indistinguishable from a placeholder.
    shared_single_source: dict[str, list[str]] = {}
    suspected: dict[str, list[str]] = {}
    for published, group in sorted(curated_by_date.items()):
        if len(group) < min_group:
            continue
        documents = {
            document
            for item in group
            if (document := _source_document(item.provenance.source_ref))
        }
        bucket = shared_single_source if len(documents) == 1 else suspected
        bucket[published] = sorted(item.id for item in group)

    suspected_count = sum(len(ids) for ids in suspected.values())
    largest: dict[str, Any] | None = None
    if shared_single_source:
        published, ids = max(
            shared_single_source.items(), key=lambda entry: len(entry[1])
        )
        largest = {
            "published_at": published,
            "count": len(ids),
            "share": round(len(ids) / len(auditable), 6) if auditable else None,
            "source_document": _source_document(
                next(
                    item.provenance.source_ref
                    for item in curated_by_date[published]
                    if _source_document(item.provenance.source_ref)
                )
            ),
        }

    caveat = None
    if largest and (largest["share"] or 0) >= DOMINANT_DATE_SHARE:
        caveat = (
            f"{largest['count']}/{len(auditable)} entries share one document's release "
            f"date ({largest['published_at']}, {largest['source_document']}). Their "
            "dates are verifiable, but a time-held-out split can only separate that "
            "release's era from everything else, so it cannot answer whether a "
            "technique was public before a given challenge."
        )

    return {
        "root": str(root),
        # Total of the entries this audit actually reasons about.  Reference
        # material is reported beside it rather than folded in, so a corpus
        # import cannot make the date distribution look healthier (or worse)
        # than the entries that can carry a date claim.
        "total": len(auditable),
        "total_including_reference": len(items),
        "reference_corpus_count": len(items) - len(auditable),
        "missing_published_at": missing,
        "missing_count": len(missing),
        "date_distribution": {
            published: len(group)
            for published, group in sorted(
                [(key, group) for key, group in by_date.items()]
            )
        },
        "distinct_dates": len(by_date),
        "shared_single_source_dates": shared_single_source,
        "suspected_placeholder_dates": suspected,
        "suspected_placeholder_count": suspected_count,
        # Reported but never suspected: dated by the run that produced them.
        "learned_count": len(learned),
        "learned_date_distribution": _date_counts(learned),
        "largest_shared_date": largest,
        "caveat": caveat,
        # The single field an experiment harness can gate on: if this is False,
        # any time-held-out result derived from this corpus is not trustworthy.
        "time_split_usable": not missing and not suspected,
        "entries": [
            {
                "id": item.id,
                "kind": item.kind,
                "published_at": (
                    item.published_at.isoformat() if item.published_at else None
                ),
                "source_type": item.provenance.source_type,
                "source_ref": item.provenance.source_ref,
            }
            for item in items
        ],
    }


def main() -> int:
    report = audit_published_at()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["caveat"]:
        print(f"\nCAVEAT: {report['caveat']}")
    return 0 if report["time_split_usable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
