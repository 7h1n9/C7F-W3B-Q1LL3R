"""Merge the extracted vulhub payloads into the live playbook.

The two sources describe the same CVEs at very different depth.  Measured before
merging:

| source | entries | entries with a *runnable* payload |
|---|---|---|
| ``data/knowledge/vulhub.jsonl`` (existing) | 151 | **32** |
| extracted from the vendored READMEs | 297 | **263** |

The old file is mostly a card index -- ``payload_template: "POST /geoserver/
TestWfsPost"`` with the real request left in a README the Worker has to go read.
The new extraction carries the request itself.

Merge rule, derived from that measurement rather than from taste:

* **Same id: the more runnable payload wins.**  101 ids collide.  New is better
  on 73, equal on 25, worse on **0**.  So the new entry replaces the old one
  unless the old one is runnable and the new one is not -- a case that does not
  currently occur but must not silently lose data if it ever does.
* **Old-only ids are kept.**  48 entries exist only in the old file (the
  hand-curated CVE playbook written earlier, e.g. CVE-2016-4977 SpEL).  Nothing
  in the extraction supersedes those.
* **``verified_solve`` is preserved across a replacement.**  That flag records
  *this project measured a solve*, which the README cannot know.  Dropping it
  would demote a proven recipe to an unproven one.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "knowledge" / "vulhub.jsonl"
EXTRACTED = ROOT / "data" / "knowledge" / "candidates" / "vulhub-payloads.jsonl"
#: Additional extraction outputs, merged in order.  Each is a different corpus
#: with its own layout but the same entry schema, so one merge loop handles all
#: of them; later sources lose collisions, which keeps vulhub canonical (it is
#: the corpus the arena images are built from).
EXTRA_SOURCES = (ROOT / "data" / "knowledge" / "candidates" / "awesome-poc-payloads.jsonl",)


def is_runnable(entry: dict) -> bool:
    """Does this entry carry a payload that can be sent, or only an endpoint?

    ``"POST /geoserver/TestWfsPost"`` is an endpoint card.  A request line plus
    headers, a query string with a value, a curl call, or a multi-line body is a
    payload.  This is the same test used to measure 32/151 vs 263/297.
    """

    payload = entry.get("payload_template") or ""
    if len(payload) <= 60:
        return False
    return bool(
        re.search(r"HTTP/1\.[01]|\?[^\s]*=|curl\s|-{1,2}[a-zA-Z]+\s|\n", payload)
    )


def is_curated(entry: dict) -> bool:
    """Was this entry written by hand, as opposed to mechanically imported?

    Runnable-payload alone is the wrong test for which version to keep.  A
    measured regression proved it: replacing ``vulhub:weblogic:cve-2018-2628``
    with the auto-extracted version made ``exploit_stop_loss_gate`` go silent,
    because the curated entry's ``known_failures`` carried "★★★★ **不需要反连**：
    这题的正解是两次 HTTP" -- a measurement about *this* box that no README can
    contain.  Gate logic reads semantics, and semantics cannot be re-extracted
    from a README.

    Signals, any of which marks an entry as hand-written:
      * a mechanism-import failure line is absent from ``known_failures``
      * ``known_failures`` mentions a measurement (★ or 反连 / 回显 / 实测)
      * ``source_ref`` is the curated CVE playbook rather than ``vulhub``
    """

    failures = " ".join(str(x) for x in (entry.get("known_failures") or []))
    if "Imported mechanically" in failures or "机制导入" in failures:
        return False
    if re.search(r"★|反连|回显|实测|measured|no callback", failures):
        return True
    return str((entry.get("provenance") or {}).get("source_ref") or "") != "vulhub"


def cve_of(entry: dict) -> str | None:
    """The CVE an entry is about, from its id/title/stack, or ``None``."""
    blob = " ".join(
        str(x)
        for x in [
            entry.get("id"),
            entry.get("title"),
            " ".join(entry.get("target_stack") or []),
        ]
    )
    match = re.search(r"CVE-\d{4}-\d{4,}", blob, re.I)
    return match.group(0).upper() if match else None


def curated_playbook_cves() -> set[str]:
    """CVEs already covered by the hand-written ``cve-playbook.jsonl``.

    This file is a *separate* input to the matcher, and its entries include
    ``negative`` ones -- measurements of routes that do **not** work.  A new
    extract for the same CVE does not overwrite those, it *competes* with them
    for ``MAX_ENTRIES = 2``, and the file-order sort puts ``vulhub:`` ahead of
    ``cve-``.  Adding CVE-2018-2628's extraction therefore pushed
    ``cve-2018-2628-weblogic-t3-negative`` off the list and silenced
    ``exploit_stop_loss_gate`` on exactly the box that gate was written for.

    A curated CVE is left entirely alone: one good recipe per CVE is all the
    budget can use anyway, and the curated one carries measurements a README
    cannot contain.
    """

    path = ROOT / "data" / "knowledge" / "cve-playbook.jsonl"
    if not path.is_file():
        return set()
    cves: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cve = cve_of(json.loads(line))
        if cve:
            cves.add(cve)
    return cves


def merge(old: list[dict], new: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Union by id, with the curated CVE playbook protected per *CVE*.

    Two collisions must be handled, and only the second one is obvious:

    1. **Same id.**  Replacing is safe; the id names one recipe.
    2. **Same CVE, different id.**  This is the one that broke a test.  Adding
       ``vulhub:weblogic:cve-2018-2628`` next to the curated
       ``cve-2018-2628-weblogic-t3-negative`` does not overwrite anything, but
       ``_pick_entries`` spends a budget of ``MAX_ENTRIES = 2`` per box and
       iterates the CVE bucket in file order.  ``vulhub:`` sorts before
       ``cve-``, so the extracted duplicate took the slot, the curated entry
       fell off, and ``exploit_stop_loss_gate`` went silent -- the exact
       regression the suite caught.

    So a CVE that the curated playbook already covers is left alone.  The
    curated entry carries measurements a README cannot ("这题的正解是两次
    HTTP"), and one good recipe per CVE is all the budget allows anyway.
    """

    stats = {"replaced": 0, "kept_old": 0, "added": 0, "old_only": 0,
             "dropped_old": 0, "enriched": 0, "cve_already_curated": 0}

    curated_cves: set[str] = set()
    for entry in old:
        if str((entry.get("provenance") or {}).get("source_ref") or "") == "cve-playbook":
            cve = cve_of(entry)
            if cve:
                curated_cves.add(cve)
    # A CVE the separate hand-written playbook covers is off limits entirely.
    curated_cves |= curated_playbook_cves()

    by_id: dict[str, dict] = {}
    by_cve: dict[str, str] = {}      # CVE -> id currently holding the slot

    for entry in old:
        by_id[entry["id"]] = entry
        stats["old_only"] += 1
        cve = cve_of(entry)
        if cve and cve not in by_cve:
            by_cve[cve] = entry["id"]

    for entry in new:
        existing = by_id.get(entry["id"])
        if existing is not None:
            # Same recipe, two descriptions: keep the curated semantics and
            # borrow the runnable payload if the curated one lacked it.
            if is_curated(existing):
                if not is_runnable(existing) and is_runnable(entry):
                    merged = dict(existing)
                    merged["payload_template"] = entry["payload_template"]
                    by_id[entry["id"]] = merged
                    stats["enriched"] += 1
                else:
                    stats["kept_old"] += 1
                continue
            if is_runnable(entry) or not is_runnable(existing):
                merged = dict(entry)
                if existing.get("verified_solve"):
                    merged["verified_solve"] = True
                    merged["trust_level"] = existing.get("trust_level", merged["trust_level"])
                by_id[entry["id"]] = merged
                stats["replaced"] += 1
            else:
                stats["kept_old"] += 1
            continue

        # Different id, same CVE: the slot is already spent.
        cve = cve_of(entry)
        if cve and cve in curated_cves:
            stats["cve_already_curated"] += 1
            continue
        if cve and cve in by_cve:
            # Already have a recipe for this CVE under another id; keep the one
            # with the runnable payload, drop the other.
            held = by_id[by_cve[cve]]
            if is_runnable(held) and not is_runnable(entry):
                stats["cve_already_curated"] += 1
                continue
            if is_runnable(entry) and not is_runnable(held):
                del by_id[held["id"]]
                by_id[entry["id"]] = entry
                by_cve[cve] = entry["id"]
                stats["replaced"] += 1
                continue
            # Both runnable: keep the first, they describe the same CVE.
            stats["cve_already_curated"] += 1
            continue

        by_id[entry["id"]] = entry
        if cve:
            by_cve[cve] = entry["id"]
        stats["added"] += 1

    # Deterministic order, so a diff after a re-run shows real changes only.
    ordered = [by_id[key] for key in sorted(by_id)]
    return ordered, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", default=str(LIVE))
    parser.add_argument("--extracted", default=str(EXTRACTED))
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    live_path = Path(args.live)
    old = [json.loads(line) for line in live_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    sources = [Path(args.extracted), *EXTRA_SOURCES]
    merged = old
    total_stats: dict[str, int] = {}
    for source in sources:
        if not source.is_file():
            print(f"（跳过不存在的来源：{source}）")
            continue
        new = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        merged, stats = merge(merged, new)
        for key, value in stats.items():
            total_stats[key] = total_stats.get(key, 0) + value

    stats = total_stats
    new = []  # only used for the length report below

    before = sum(1 for e in old if is_runnable(e))
    after = sum(1 for e in merged if is_runnable(e))
    print(
        f"合并前 {len(old)} 条（可用载荷 {before}）→ 合并后 {len(merged)} 条（可用载荷 {after}）\n"
        f"  替换 {stats['replaced']}；新增 {stats['added']}；保留旧版 {stats['kept_old']}；"
        f"精修补载荷 {stats['enriched']}；同 CVE 已有条目跳过 {stats['cve_already_curated']}"
    )

    # Nothing unverified may claim a solve, and ids must stay unique -- both
    # would break the matcher rather than merely look wrong.
    ids = [e["id"] for e in merged]
    assert len(ids) == len(set(ids)), "duplicate ids after merge"
    assert all(isinstance(e.get("verified_solve"), bool) for e in merged)

    if args.dry_run:
        print("（dry-run，未写入）")
        return 0

    if not args.no_backup:
        backup = live_path.with_suffix(live_path.suffix + ".bak")
        shutil.copy2(live_path, backup)
        print(f"备份原文件 → {backup}")

    live_path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in merged) + "\n", encoding="utf-8"
    )
    print(f"写入 {live_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
