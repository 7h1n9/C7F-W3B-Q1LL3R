"""Technique evidence: measured hit rates that can reorder candidate techniques.

Why this module exists
----------------------
``technique_candidates()`` ranks candidate techniques by *corpus size* -- "a
class with 25 entries is one the corpus has thought about".  That prior has
nothing to do with whether the technique works.  Measured on the 2026-11-03
export, ``Command injection and RCE`` has 9 corpus entries (5th place, so it
never survives the 3-slot cut) while it is the single most productive class in
practice: 10 accepted runs gained a fact from OGNL/RCE against 6 that hit a
wall.

This module turns the transition export into a *measured* prior so the ranking
can use it.  Three properties make it safe to switch on:

* **It only reorders, never adds.**  A technique that is not already a
  candidate stays out; the evidence cannot invent a class.  This keeps the
  change reversible by deleting one file.
* **It needs corroboration.**  A class with fewer than ``MIN_EVIDENCE_RUNS``
  distinct runs keeps its original corpus-size rank.  ``sqli.union`` has 2 fact
  runs out of 3 -- real, but far too few to let it outrank a class with 25
  entries on its own.
* **It needs a rate floor.**  Evidence promotes a class past classes that lack
  it, but only when the class clears ``MIN_EVIDENCE_RATE`` as well as the run
  count -- a 1-in-2 fluke must not outrank a class with 25 corpus entries.

The boost is a *tie-breaker above the corpus count*, not a replacement for it:
a class with strong evidence but almost no corpus backing still needs the
corpus to supply a probe worth sending.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

#: Evidence labels (``app.training.candidates``) mapped onto the corpus
#: vocabulary used by ``known_exploit._TECHNIQUE_PROBES``.  Two namespaces
#: exist because one describes *what a command does* and the other *which probe
#: to send*; a mapping is unavoidable and belongs here, in one auditable place,
#: rather than as string matching scattered through the ranker.
EVIDENCE_TO_CANDIDATE: dict[str, tuple[str, ...]] = {
    "rce.ognl": ("Command injection and RCE",),
    "rce.deser": ("PHP deserialization",),
    "lfi.traversal": ("Local file inclusion and PHP wrappers",),
    "lfi.wrapper": ("Local file inclusion and PHP wrappers",),
    "sqli.union": ("SQL injection",),
    "sqli.boolean": ("SQL injection",),
    "sqli.time": ("SQL injection",),
    "ssti.eval": ("Server-side template injection",),
    "ssrf.internal": ("Server-side request forgery",),
    "upload.bypass": ("File upload bypass",),
    "auth.jwt": ("JWT forgery",),
    # Recon labels are deliberately absent.  `recon.dir` / `recon.port` measure
    # whether enumeration finds anything -- a property of the box, not a
    # candidate exploit class -- and `recon.source` means "look for backup or
    # source files", which is not the same thing as "audit PHP source"
    # (`PHP source review`).  Mapping any of them would let "gobuster found a
    # path" promote an exploit technique, which is a category error.
}

#: Evidence labels that describe reconnaissance rather than exploitation.  Kept
#: explicit so a future mapping cannot silently reintroduce them.
NON_CANDIDATE_EVIDENCE: frozenset[str] = frozenset(
    {"recon.dir", "recon.port", "recon.source"}
)

#: Distinct runs required before evidence may reorder anything.
MIN_EVIDENCE_RUNS = 4

#: Minimum hit rate before evidence may promote a class.  Guards against a
#: 1-in-2 fluke outranking a class with 25 corpus entries.
MIN_EVIDENCE_RATE = 0.30

DEFAULT_EVIDENCE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "knowledge" / "technique-evidence.json"
)


def load_evidence(path: Path | str | None = None) -> dict[str, dict]:
    """Read the evidence file; a missing or broken file means "no evidence".

    Never raises: this feeds a ranking that runs inside the solve path, and a
    malformed file must degrade to the old behaviour rather than fail a Run.
    """

    target = Path(path) if path is not None else DEFAULT_EVIDENCE_PATH
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    techniques = payload.get("techniques") if isinstance(payload, dict) else None
    if not isinstance(techniques, dict):
        return {}
    out: dict[str, dict] = {}
    for label, row in techniques.items():
        if not isinstance(row, dict):
            continue
        fact = int(row.get("runs_fact") or 0)
        dead = int(row.get("runs_dead") or 0)
        if fact + dead <= 0:
            continue
        out[str(label)] = {
            "runs_fact": fact,
            "runs_dead": dead,
            "rate": fact / (fact + dead),
        }
    return out


def candidate_evidence(
    evidence: Mapping[str, Mapping] | None = None,
) -> dict[str, dict]:
    """Collapse evidence labels onto candidate names.

    Several evidence labels can describe one candidate (three SQLi variants all
    feed ``SQL injection``), so their run counts are summed.  Summing runs
    rather than averaging rates avoids letting a 2-run label drag down a 10-run
    one.

    Accepts either the raw evidence labels (``rce.ognl``) or a mapping that is
    already keyed by candidate name (``Command injection and RCE``).  The second
    form is what a caller gets back from this function, and re-merging it must
    be idempotent -- an earlier version re-applied the label mapping to
    already-mapped keys, which silently produced an empty result and made the
    whole ranking a no-op.
    """

    source = evidence if evidence is not None else load_evidence()
    merged: dict[str, dict] = {}
    for key, row in source.items():
        # An already-candidate-keyed row passes through unchanged.
        targets = EVIDENCE_TO_CANDIDATE.get(key) or ((key,) if key else ())
        for candidate in targets:
            entry = merged.setdefault(candidate, {"runs_fact": 0, "runs_dead": 0})
            entry["runs_fact"] += int(row.get("runs_fact") or 0)
            entry["runs_dead"] += int(row.get("runs_dead") or 0)
    for entry in merged.values():
        total = entry["runs_fact"] + entry["runs_dead"]
        entry["rate"] = entry["runs_fact"] / total if total else 0.0
        entry["runs"] = total
    return merged


def apply_evidence_boost(
    ranked: Sequence[tuple[int, str]],
    evidence: Mapping[str, Mapping] | None = None,
    *,
    min_runs: int = MIN_EVIDENCE_RUNS,
    min_rate: float = MIN_EVIDENCE_RATE,
) -> list[tuple[int, str]]:
    """Reorder ``(corpus_count, name)`` by measured evidence.

    The output keeps the original tuple shape so the caller's downstream code
    (which formats ``count`` into the probe line) needs no change.

    Implemented as promotion above the evidence-less classes rather than a full
    re-sort: a class either clears both thresholds and moves ahead of every
    class that does not, or keeps its corpus-size position.  Within the promoted
    group the order is by hit rate, so the corpus count still breaks ties.
    """

    merged = candidate_evidence(evidence)
    ordered = list(ranked)
    if not ordered:
        return ordered

    # Promotion is decided by *evidence strength*, not by a relative shift.
    #
    # The first version lifted a class at most ``max_boost`` places, which
    # measured out as a no-op: with 9 candidates and a 3-slot cut, "up to 3
    # places" cannot move the 5th-place class into the list at all, so the
    # evidence was silently ignored.  A rank-shift is the wrong unit here --
    # what matters is whether a class deserves to be promoted past classes that
    # merely have more corpus entries.
    #
    # So: a class with at least ``min_runs`` runs AND a hit rate of at least
    # ``min_rate`` is promoted above every class that lacks such evidence.  The
    # rate floor is what stops a 1-in-2 fluke from outranking a 25-entry class.
    def backed(name: str) -> bool:
        row = merged.get(name)
        return (
            row is not None
            and row.get("runs", 0) >= min_runs
            and float(row.get("rate", 0.0)) >= min_rate
        )

    promoted = [item for item in ordered if backed(item[1])]
    rest = [item for item in ordered if not backed(item[1])]
    if not promoted:
        return ordered
    promoted.sort(key=lambda item: (-float(merged[item[1]]["rate"]), -int(merged[item[1]]["runs_fact"])))
    return promoted + rest


def write_evidence(techniques: Mapping[str, Mapping], path: Path | str | None = None) -> Path:
    """Persist evidence in the shape :func:`load_evidence` reads."""

    target = Path(path) if path is not None else DEFAULT_EVIDENCE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source": "data/training transitions (train split, accepted runs)",
        "min_evidence_runs": MIN_EVIDENCE_RUNS,
        "min_evidence_rate": MIN_EVIDENCE_RATE,
        "techniques": {
            label: {
                "runs_fact": int(row.get("runs_fact") or 0),
                "runs_dead": int(row.get("runs_dead") or 0),
            }
            for label, row in techniques.items()
        },
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
