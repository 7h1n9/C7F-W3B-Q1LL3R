#!/usr/bin/env python3
"""Mine the facts that past Runs recorded against a box, for reuse next time.

Why this exists
---------------
``_order_by_history`` decides *which* box to run next but never carries anything
across Runs: every attempt on a box starts from a blank blackboard.  Measured on
``资产保修核验平台`` (29 Runs), successive Runs' fact sets overlap at Jaccard
0.25-0.37 — the later attempts keep rediscovering the same endpoints.  That
repetition is the waste this script removes.

What it does NOT do
-------------------
It does not verify anything.  A fact lifted here is a *previous Run's
observation*, which may have been wrong, may describe a dead end, or may have
been recorded against a different build of the box.  Everything it emits is
labelled as an unverified prior observation and must be re-confirmed against the
live target.

The 89% problem
---------------
Counting naively, the 90 targeted boxes hold 18,752 facts of >60 chars.  Only
**732** are real reconnaissance.  The rest are:

* ``REVIEW_NEEDED fact_id=N; reason=...``            (61%, process noise)
* ``{"endpoint": "/x", "request_count": 4, ...}``    (28%, telemetry)

Feeding the raw set into a prompt would bury the useful line under 25,000 chars
of bookkeeping.  Hence ``_is_junk`` below; it is the load-bearing filter, not a
nicety.

A trap worth naming: two Runs on the same box produced *byte-identical* fact
sets (Jaccard 1.00) whose entire content was

    {"type":"turn.failed","error":{"message":"... 401 Unauthorized ..."}}

Identical engine failures look exactly like identical knowledge.  Without the
``turn.failed`` / ``401`` exclusions, the miner would confidently publish "this
box returns 401" as a finding.

Usage
-----
    python scripts/mine_box_facts.py --database-url mysql+asyncmy://... \
        [--out data/derived/box-facts.jsonl] [--dry-run] [--show N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_OUT = ROOT / "data" / "derived" / "box-facts.jsonl"

#: Facts shorter than this carry no actionable content — they are labels like
#: "Found the login page", which the next Run rediscovers in one request anyway.
MIN_FACT_CHARS = 80

#: Cap per box.  The goal is a prompt-sized briefing, not an archive.
#:
#: Tuned against the real corpus: at 12 facts the median box carried 932 chars
#: but 17 boxes ran past 1,500, which is the ceiling the renderer allows for the
#: whole block.  Eight facts keeps every box under that ceiling with room for the
#: header, and lands the briefing at roughly the size of one checklist section
#: rather than three.
MAX_FACTS_PER_BOX = 8

#: Per-fact truncation.  Matches the renderer's own per-entry budget so a mined
#: fact cannot blow the block budget on its own.
MAX_FACT_CHARS = 220

#: Hard ceiling on a box's total briefing.  Facts are taken in order until the
#: next one would exceed this, so a box with many short facts still gets a fuller
#: briefing than one with a few long ones.  Measured: without this, five boxes ran
#: to 1,653 chars and pushed the checklist that follows further down the prompt
#: than the facts were worth.
MAX_BOX_CHARS = 1400

#: A fact must mention one of these to count as reconnaissance.  Without it the
#: filter passes process chatter that happens to be long -- "Native Worker
#: completed a bounded evidence-backed observation" is >80 chars and contains
#: nothing.
_SIGNAL = re.compile(
    r"(?i)("
    r"accepts|returns|requires|rejects|responds|discloses|exposes|"
    r"endpoint|route|param(?:eter)?s?|field|header|cookie|token|credential|"
    r"password|payload|status|bypass|traversal|injection|deserial|"
    r"HTTP\s*\d{3}|\d{3}\s*(?:OK|Found|Forbidden|Unauthorized)|"
    r"发现|存在|绕过|漏洞|注入|上传|凭据"
    r")"
)

#: Records that are *about the harness*, not the target.  Every entry here was
#: observed in the real data; the list is deliberately explicit rather than a
#: loose regex so a new noise shape is a deliberate addition.
_JUNK_PREFIX = re.compile(
    r"^(?:"
    r"REVIEW_NEEDED"
    r"|Native Worker completed"
    r"|Worker completed"
    r"|ROUND_BUDGET"
    r"|\[(?:codex|claude)\]\s*\{?\"?type\"?\s*:\s*\"?(?:turn\.failed|error)"
    r")",
    re.I,
)

#: Substrings that mark a record as an infrastructure failure even when it is
#: wrapped in prose.  These are what produced the Jaccard-1.00 false positive.
_JUNK_CONTAINS = re.compile(
    r"(?i)("
    r"401 Unauthorized|Missing bearer|invalid_api_key|"
    r"rate limit|ECONNREFUSED|ENOTFOUND|socket hang up|"
    r"turn\.failed|ROUND_BUDGET_EXHAUSTED|budget exhausted|"
    r"no output|timed out|timeout after"
    r")"
)

#: Telemetry masquerading as a fact: pure JSON of request bookkeeping.
_TELEMETRY = re.compile(r"^\s*\{.*\"(request_count|endpoint_key|evidence_refs)\"")


@dataclass
class BoxFacts:
    box_name: str
    facts: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "box_name": self.box_name,
            "facts": self.facts,
            "mined_from_runs": self.runs,
            "verified": False,
            "note": (
                "Prior observation from an earlier Run on this box. Unverified: "
                "re-confirm against the live target before relying on it."
            ),
        }


def _normalise(text: str) -> str:
    """Collapse whitespace and strip the agent's own name tag.

    ``[codex] foo`` and ``[claude] foo`` are the same observation recorded by two
    engines; keeping the tag would let both survive deduplication.
    """

    text = re.sub(r"^\s*\[(?:codex|claude|worker[-\w]*)\]\s*", "", text.strip())
    return re.sub(r"\s+", " ", text)


def _dedupe_key(text: str) -> str:
    """A cheap content key: letters and digits only, first 120 chars.

    Aggressive on purpose.  Two facts that differ only in punctuation and quoting
    are the same fact, and keeping both wastes the per-box budget.
    """

    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())[:120]


def is_usable(text: str) -> bool:
    """Is this record a reusable observation about the target?"""

    if len(text) < MIN_FACT_CHARS:
        return False
    if _JUNK_PREFIX.match(text) or _JUNK_CONTAINS.search(text):
        return False
    if _TELEMETRY.match(text):
        return False
    return bool(_SIGNAL.search(text))


def extract_facts(payload_json: str) -> str:
    """Pull the fact text out of a ``muteki.fact_added`` payload.

    The payload nests twice: ``{"payload": {"payload": {"fact": ...}}}``.  Some
    emitters skip the inner level, so both shapes are accepted rather than
    assuming one and silently returning "".
    """

    try:
        outer = json.loads(payload_json)
    except (TypeError, ValueError):
        return ""
    inner = outer.get("payload") or {}
    if isinstance(inner, dict) and isinstance(inner.get("payload"), dict):
        inner = inner["payload"]
    if not isinstance(inner, dict):
        return ""
    return _normalise(str(inner.get("fact") or ""))


async def load_targets(database_url: str) -> list[dict]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.challenge import Challenge

    engine = create_async_engine(database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            rows = (await session.scalars(select(Challenge))).all()
            out = [
                {"id": str(r.id), "name": str(r.name), "status": str(r.status)}
                for r in rows
                if str(r.status) == "ACTIVE"
            ]
    finally:
        await engine.dispose()
    return out


async def load_facts(database_url: str) -> dict[str, list[tuple[str, str]]]:
    """Map challenge_id -> [(run_id, fact_text)] for every recorded fact."""

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        async with maker() as session:
            result = await session.execute(
                text(
                    """
                    select r.challenge_id as cid, e.run_id as rid, e.payload_json as pj
                    from run_events e
                    join solve_runs r on r.id = e.run_id
                    where e.event_type = 'muteki.fact_added'
                    """
                )
            )
            for row in result:
                fact = extract_facts(row.pj)
                if not fact:
                    continue
                out.setdefault(str(row.cid), []).append((str(row.rid), fact))
    finally:
        await engine.dispose()
    return out


def mine_one(name: str, raw: list[tuple[str, str]]) -> BoxFacts:
    """Reduce one box's raw fact stream to a short, deduplicated briefing."""

    seen: set[str] = set()
    kept: list[str] = []
    runs: list[str] = []
    used = 0
    for run_id, fact in raw:
        # Normalise before filtering: the engine tag is part of the record, not
        # part of the observation, and leaving it in defeats both the dedupe key
        # and the length budget.
        fact = _normalise(fact)
        if not is_usable(fact):
            continue
        key = _dedupe_key(fact)
        if not key or key in seen:
            continue
        piece = fact[:MAX_FACT_CHARS]
        if used + len(piece) > MAX_BOX_CHARS:
            # Stop rather than truncate: a half-sentence is worse than one fewer
            # observation, because the reader cannot tell it was cut.
            break
        seen.add(key)
        kept.append(piece)
        used += len(piece)
        if run_id not in runs:
            runs.append(run_id)
        if len(kept) >= MAX_FACTS_PER_BOX:
            break
    return BoxFacts(box_name=name, facts=kept, runs=runs[:MAX_FACTS_PER_BOX])


def needs_recipes_only(targets: list[dict]) -> list[dict]:
    """Filter to the boxes that have no imported recipe.

    A box with a vulhub/Awesome-POC entry already receives a concrete payload in
    its FIRST ACTION; a pile of historical prose would compete with it for the
    same prompt budget and win nothing.
    """

    sys.path.insert(0, str(ROOT / "backend"))
    from types import SimpleNamespace

    from app.services.known_exploit import _pick_entries

    out = []
    for t in targets:
        ch = SimpleNamespace(name=t["name"], description="点击启动靶机。")
        picked = _pick_entries(ch)
        imported = [
            e
            for e in picked
            if str((e.get("provenance") or {}).get("source_ref") or "")
            in ("vulhub", "awesome-poc")
        ]
        if not imported:
            out.append(t)
    return out


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", type=int, default=0)
    args = parser.parse_args(argv)

    targets = await load_targets(args.database_url)
    targets = needs_recipes_only(targets)
    print(f"无导入配方的 ACTIVE 题: {len(targets)}")

    raw = await load_facts(args.database_url)
    print(f"有 fact 记录的题: {len(raw)}")

    mined: list[BoxFacts] = []
    total_facts = 0
    for t in targets:
        rows = raw.get(t["id"]) or []
        if not rows:
            continue
        bf = mine_one(t["name"], rows)
        if bf.facts:
            mined.append(bf)
            total_facts += len(bf.facts)

    print(f"挖掘结果: {len(mined)} 道题, {total_facts} 条可用 fact")
    for bf in sorted(mined, key=lambda b: -len(b.facts))[: args.show or 5]:
        print(f"\n=== {bf.box_name} ({len(bf.facts)} 条, runs={len(bf.runs)}) ===")
        for f in bf.facts[:3]:
            print(f"   • {f[:150]}")

    if args.dry_run:
        print("\n[dry-run] 未写文件")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for bf in mined:
            fh.write(json.dumps(bf.as_dict(), ensure_ascii=False) + "\n")
    print(f"\n写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
