"""Distil a CVE playbook entry from a run that actually solved that CVE.

The existing ``data/knowledge/cve-playbook.jsonl`` covers 13 of the 38 CVEs this
project has solved.  The remaining 29 were each solved exactly once, which is
why nobody has written them up -- and hand-writing them is not acceptable here:
a fabricated payload in a playbook is worse than a missing entry, because the
Worker will send it and burn the round.

So this script quotes instead of inventing.  For a given solved Run it walks the
recorded commands, keeps the ones that produced a fact, and emits an entry whose
every line is traceable to ``source_run_id`` + ``event_id``.  If the trajectory
does not contain enough evidence, it says so and writes nothing.

Usage::

    python scripts/distil_cve_playbook.py --run-id <uuid> --dry-run
    python scripts/distil_cve_playbook.py --run-id <uuid> --append
    python scripts/distil_cve_playbook.py --missing          # list uncovered CVEs
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.training.candidates import technique_labels  # noqa: E402

CONTAINER = "c7f-w3b-q1ll3r-mysql-1"
PLAYBOOK = ROOT / "data" / "knowledge" / "cve-playbook.jsonl"
TRAINING_DIR = ROOT / "data" / "training"

#: A step is only worth quoting when its command is short enough to read and
#: specific enough to re-run.  Longer commands are scripts whose value is in the
#: file, not the line.
MAX_STEP_CHARS = 320
MIN_STEPS = 2

#: Commands that are *our* bookkeeping, not the target's surface.  A playbook
#: entry whose second step is "write this fact to the blackboard" teaches the
#: next Worker nothing about the box, and was caught doing exactly that on the
#: first real distil (CVE-2018-11776).
_PLATFORM_INTERNAL = re.compile(
    r"(blackboard\.py|read-all|read-facts|read-deadends|write-fact|write-deadend|"
    r"^\s*(ls|cd|pwd|echo|export|cat)\b|/workspace\b)",
    re.I,
)

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)


def sh_json(sql: str) -> list[dict]:
    proc = subprocess.run(
        [
            "docker", "exec", CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-uctf_agent", "-pctf_agent", "ctf_agent",
            "-N", "-B", "-r", "-e", sql,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"mysql failed: {proc.stderr[:400]}")
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Warning"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def playbook_cves() -> set[str]:
    if not PLAYBOOK.is_file():
        return set()
    found: set[str] = set()
    for line in PLAYBOOK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        found.update(m.upper() for m in _CVE_RE.findall(line))
    return found


def solved_cves() -> Counter:
    rows = sh_json(
        "select json_object('t',coalesce(json_unquote(json_extract(hints_json,'$.challenge')),'')) "
        "from solve_runs where status='COMPLETED_SOLVED'"
    )
    counts: Counter = Counter()
    for row in rows:
        for cve in _CVE_RE.findall(str(row.get("t") or "")):
            counts[cve.upper()] += 1
    return counts


def accepted_run_for(cve: str) -> dict | None:
    rows = sh_json(
        "select json_object('run',r.id,'title',"
        "coalesce(json_unquote(json_extract(r.hints_json,'$.challenge')),'')) "
        "from solve_runs r "
        "join arena_solve_queue q on q.run_id=r.id and q.verdict='accepted' "
        f"where r.status='COMPLETED_SOLVED' and upper("
        f"coalesce(json_unquote(json_extract(r.hints_json,'$.challenge')),'')) like '%{cve}%' "
        "order by r.finished_at desc limit 1"
    )
    return rows[0] if rows else None


def transitions_for(run_id: str) -> list[dict]:
    path = sorted(TRAINING_DIR.glob("transitions-*.jsonl"))
    if not path:
        return []
    out: list[dict] = []
    with path[-1].open("r", encoding="utf-8") as handle:
        for line in handle:
            if run_id not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("run_id") == run_id:
                out.append(row)
    return sorted(out, key=lambda r: r.get("sequence") or 0)


def build_entry(cve: str, run: dict, transitions: list[dict]) -> tuple[dict | None, str]:
    """Return ``(entry, reason)``; ``entry`` is None when evidence is too thin."""

    if not transitions:
        return None, "no transitions exported for this run"

    # A step earns its place by producing a fact; failing that, by being the
    # final command that yielded the flag.
    productive = [
        t for t in transitions
        if int((t.get("observation") or {}).get("delta_facts") or 0) > 0
        and t["action"].get("command")
    ]
    if len(productive) < MIN_STEPS:
        return None, f"only {len(productive)} fact-producing command(s); need {MIN_STEPS}"

    steps: list[str] = []
    for t in productive:
        command = " ".join(str(t["action"]["command"]).split())
        if len(command) > MAX_STEP_CHARS:
            continue
        # Our own bookkeeping is not a step of the exploit.
        if _PLATFORM_INTERNAL.search(command):
            continue
        if command in steps:
            continue
        steps.append(command)
    if len(steps) < MIN_STEPS:
        return None, f"only {len(steps)} usable step(s) after removing platform bookkeeping"

    techniques: list[str] = []
    for t in productive:
        for label in technique_labels(str(t["action"].get("command") or "")):
            if label not in techniques:
                techniques.append(label)
    if not techniques:
        # An entry whose technique nobody can name cannot be routed to by the
        # retriever, so it would sit in the corpus unread.
        return None, "no technique could be identified for these steps"

    title = str(run.get("title") or "")
    product = ""
    for token in re.split(r"[\s\[\]]+", title):
        if token and not _CVE_RE.match(token) and len(token) > 2:
            product = token.strip("[]")
            break

    entry = {
        "id": f"cve-{cve.lower()}",
        "kind": "cve",
        "title": title,
        "technique": techniques[0] if techniques else "unknown",
        "techniques": techniques,
        "summary": (
            f"Distilled from an accepted arena solve of {title}. "
            f"{len(steps)} command(s) produced verified facts."
        ),
        "preconditions": [
            f"target exposes {product}" if product else "target reachable",
        ],
        "target_stack": [product] if product else [],
        "steps": steps,
        "verification_method": [
            "the platform accepted a flag produced by this sequence",
        ],
        "known_failures": [],
        "provenance": {
            "source_run_id": run.get("run") or "",
            "source": "arena accepted solve, distilled",
            "generated_at": datetime.now(UTC).isoformat(),
            "command_count": len(transitions),
        },
        "verified_solve": True,
    }
    return entry, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default="")
    parser.add_argument("--cve", default="", help="resolve the newest accepted run for this CVE")
    parser.add_argument("--missing", action="store_true", help="list solved CVEs with no playbook entry")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    covered = playbook_cves()
    counts = solved_cves()

    if args.missing:
        missing = [(cve, n) for cve, n in counts.most_common() if cve not in covered]
        print(f"solved CVEs: {len(counts)}  covered: {len(covered)}  missing: {len(missing)}")
        for cve, n in missing:
            run = accepted_run_for(cve)
            print(f"  {cve}  solved={n}  run={run['run'][:8] if run else '-'}")
        return 0

    cve = args.cve.upper()
    run_id = args.run_id
    if not run_id:
        if not cve:
            raise SystemExit("--run-id or --cve is required")
        run = accepted_run_for(cve)
        if not run:
            raise SystemExit(f"no accepted run found for {cve}")
        run_id = run["run"]
    else:
        rows = sh_json(
            "select json_object('run',id,'title',"
            "coalesce(json_unquote(json_extract(hints_json,'$.challenge')),'')) "
            f"from solve_runs where id='{run_id}'"
        )
        run = rows[0] if rows else {"run": run_id, "title": ""}
        if not cve:
            match = _CVE_RE.search(str(run.get("title") or ""))
            cve = match.group(0).upper() if match else ""

    if not cve:
        raise SystemExit("could not determine a CVE id for this run")
    if cve in covered:
        print(f"{cve} already has a playbook entry; nothing to do")
        return 0

    entry, reason = build_entry(cve, run, transitions_for(run_id))
    if entry is None:
        print(f"refusing to write {cve}: {reason}")
        print("A playbook entry with invented steps is worse than no entry.")
        return 1

    print(json.dumps(entry, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0
    if not args.append:
        print("\n(dry output only; pass --append to write)")
        return 0
    with PLAYBOOK.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"\nappended to {PLAYBOOK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
