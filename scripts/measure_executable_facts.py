"""Measure executable facts per run and per final status.

Read-only.  Command counts and run labels come from the newest
``data/training/transitions-*.jsonl`` (one line per command); fact text comes
from MySQL ``run_events`` (``event_type = 'muteki.fact_added'``), restricted to
the same run ids.  The transition export keeps only fact *counts*.

The headline ratio is printed together with its definition version
(``EXECUTABLE_DEFINITION_VERSION``) and, alongside it, the frozen legacy number
(``LEGACY_EXECUTABLE_DEFINITION_VERSION``).  Both are computed on the same rows
by ``executable_fact_summary``; the executable field therefore can never be
read as one number when it is the other.

Two windows are printed because the numerator and denominator are not from the
same snapshot:

* ``facts_as_of_today`` -- every fact row the current database holds for these
  runs.  This is the headline, comparable to the project's historical "N facts
  per 100 commands".
* ``facts_in_lifetime`` -- only facts whose timestamp is at or before the run's
  last ``muteki.worker_command``.  This is the only window that can be read as
  "facts produced per command"; the remainder was published in the finalisation
  batch after the trajectory ended.

``--reconcile-legacy`` applies both rules to the same facts and prints every
group where they disagree, so a change in the ratio can be attributed to a rule
change rather than guessed at.

The limitation block is printed by the script itself, not just documented.

``labels.final_status`` is matched against exact strings
(``COMPLETED_SOLVED`` / ``COMPLETED_UNSOLVED`` / ``FAILED_ENGINE``); substring
matching on ``"SOLVED"`` would fold unsolved runs into solved ones.

MySQL is read with ``-N -B -r``: the raw flag is load-bearing, because without
it the batch client re-escapes backslashes and corrupts stored JSON.  A failed
write to the metrics guard only warns (see ``_metrics_boot``).

Usage::

    python scripts/measure_executable_facts.py
    python scripts/measure_executable_facts.py --limit 20 --verbose
    python scripts/measure_executable_facts.py --reconcile-legacy
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _metrics_boot import record_analysis  # noqa: E402

from app.services.fact_quality import (  # noqa: E402
    EXECUTABLE_DEFINITION_VERSION,
    FACT_CLASSES,
    LEGACY_EXECUTABLE_DEFINITION_VERSION,
    classify_fact,
    classify_fact_legacy,
    executable_fact_summary,
    is_executable_fact_legacy,
)

MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = "3307"
MYSQL_USER = "ctf_agent"
MYSQL_PASSWORD = "ctf_agent"
MYSQL_DB = "ctf_agent"
DOCKER_CONTAINER = "c7f-w3b-q1ll3r-mysql-1"

RUN_CHUNK = 25  # long IN lists have been SIGTERM'd on this box
SAMPLE_CHARS = 300
RECONCILE_CHARS = 80
RECONCILE_EXAMPLES = 10


@dataclass
class FactRecord:
    text: str
    created_at: str
    verified: bool


@dataclass
class RunSlice:
    run_id: str
    status: str
    commands: int = 0
    export_facts: int = 0  # max ``state.facts`` in the transition export
    last_command_at: str = ""
    facts: list[FactRecord] = field(default_factory=list)

    def lifetime_facts(self) -> list[FactRecord]:
        if not self.last_command_at:
            return []
        return [fact for fact in self.facts if fact.created_at <= self.last_command_at]


# ---------------------------------------------------------------------------
# transitions: command denominator and labels
# ---------------------------------------------------------------------------


def newest_transitions() -> Path:
    candidates = sorted((ROOT / "data" / "training").glob("transitions-*.jsonl"))
    if not candidates:
        raise SystemExit("no transitions-*.jsonl under data/training/")
    return candidates[-1]


def read_transitions(path: Path, limit_runs: int) -> list[RunSlice]:
    """Group rows by run, preserving file order.

    The exporter writes each run as one contiguous block, so a new run id after
    ``limit_runs`` blocks means we can stop reading.
    """

    slices: dict[str, RunSlice] = {}
    order: list[str] = []
    current: str | None = None

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            run_id = str(row.get("run_id") or "")
            if not run_id:
                continue
            if run_id != current:
                if current is not None and limit_runs and len(order) >= limit_runs:
                    break
                current = run_id
                if run_id not in slices:
                    labels = row.get("labels") or {}
                    slices[run_id] = RunSlice(
                        run_id=run_id,
                        status=str(labels.get("final_status") or "UNKNOWN"),
                    )
                    order.append(run_id)
            run = slices[run_id]
            run.commands += 1
            state = row.get("state") or {}
            try:
                run.export_facts = max(run.export_facts, int(state.get("facts") or 0))
            except (TypeError, ValueError):
                pass

    return [slices[run_id] for run_id in order]


# ---------------------------------------------------------------------------
# MySQL: fact rows and last-command timestamps
# ---------------------------------------------------------------------------


def mysql_json(sql: str, transport: str) -> list[dict[str, Any]]:
    if transport == "docker":
        command = [
            "docker", "exec", DOCKER_CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            f"-u{MYSQL_USER}", f"-p{MYSQL_PASSWORD}", MYSQL_DB,
            "-N", "-B", "-r", "-e", sql,
        ]
    else:
        if not shutil.which("mysql"):
            raise SystemExit(
                "mysql client not found on PATH; rerun with --transport docker"
            )
        command = [
            "mysql",
            "-h", MYSQL_HOST, "-P", MYSQL_PORT,
            f"-u{MYSQL_USER}", f"-p{MYSQL_PASSWORD}", MYSQL_DB,
            "-N", "-B", "-r", "-e", sql,
        ]

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(f"mysql/docker not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise SystemExit(f"mysql query timed out after 180s: {exc}") from exc

    if proc.returncode != 0:
        raise SystemExit(f"mysql failed: {(proc.stderr or '').strip()[:400]}")

    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Warning"):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def split_fact_payload(document: dict[str, Any]) -> tuple[str | None, bool]:
    """Return ``(fact_text, verified)`` from one fact event payload.

    Measured shape: ``{"payload": {"payload": {"fact": ...}},
    "verified": bool, ...}``.  The older single-``payload`` shape is accepted
    so an old run does not silently disappear from the numerator.
    """

    verified = document.get("verified") is True
    middle = document.get("payload")
    if not isinstance(middle, dict):
        return None, verified
    inner = middle.get("payload")
    if isinstance(inner, dict) and isinstance(inner.get("fact"), str):
        return inner["fact"], verified
    if isinstance(middle.get("fact"), str):
        return middle["fact"], verified
    return None, verified


def fetch_facts(run_ids: Iterable[str], transport: str) -> dict[str, list[FactRecord]]:
    ids = list(run_ids)
    by_run: dict[str, list[FactRecord]] = {run_id: [] for run_id in ids}

    for start in range(0, len(ids), RUN_CHUNK):
        chunk = ids[start : start + RUN_CHUNK]
        id_list = ",".join(f"'{run_id}'" for run_id in chunk)
        sql = (
            "select json_object('run_id', run_id, "
            "'created_at', date_format(created_at, '%Y-%m-%dT%H:%i:%s'), "
            "'payload', payload_json) "
            "from run_events "
            f"where run_id in ({id_list}) and event_type = 'muteki.fact_added' "
            "order by created_at, id"
        )
        for row in mysql_json(sql, transport):
            run_id = str(row.get("run_id") or "")
            if run_id not in by_run:
                continue
            document = row.get("payload")
            if isinstance(document, str):
                try:
                    document = json.loads(document)
                except ValueError:
                    continue
            if not isinstance(document, dict):
                continue
            text, verified = split_fact_payload(document)
            if text is None:
                continue
            by_run[run_id].append(
                FactRecord(
                    text=text,
                    created_at=str(row.get("created_at") or ""),
                    verified=verified,
                )
            )

    return by_run


def fetch_last_commands(run_ids: Iterable[str], transport: str) -> dict[str, str]:
    ids = list(run_ids)
    last: dict[str, str] = {}

    for start in range(0, len(ids), RUN_CHUNK):
        chunk = ids[start : start + RUN_CHUNK]
        id_list = ",".join(f"'{run_id}'" for run_id in chunk)
        sql = (
            "select json_object('run_id', run_id, "
            "'last_command', date_format(max(created_at), '%Y-%m-%dT%H:%i:%s')) "
            "from run_events "
            f"where run_id in ({id_list}) and event_type = 'muteki.worker_command' "
            "group by run_id"
        )
        for row in mysql_json(sql, transport):
            run_id = str(row.get("run_id") or "")
            if run_id:
                last[run_id] = str(row.get("last_command") or "")

    return last


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _pct(numerator: int, denominator: int) -> str:
    return f"{numerator * 100 / denominator:.1f}%" if denominator else "n/a"


def _one_line(text: str, width: int = SAMPLE_CHARS) -> str:
    return " ".join(text.split())[:width]


def _rate(executable: int, commands: int) -> float:
    return round(executable * 100 / commands, 2) if commands else 0.0


def window_stats(records: list[FactRecord], commands: int) -> dict[str, Any]:
    summary = executable_fact_summary([fact.text for fact in records])
    by_class = summary["by_class"]
    assert isinstance(by_class, dict)
    return {
        "facts": len(records),
        "executable": summary["executable"],
        "ratio": summary["ratio"],
        "by_class": by_class,
        "executable_legacy": summary["executable_legacy"],
        "legacy_ratio": summary["legacy_ratio"],
        "moved_from_legacy": summary["moved_from_legacy"],
        "executable_per_100_commands": _rate(int(summary["executable"]), commands),
        "legacy_per_100_commands": _rate(int(summary["executable_legacy"]), commands),
        "summary": summary,
    }


def print_samples(runs: list[RunSlice], wanted: int = 3) -> None:
    samples: dict[str, list[str]] = {name: [] for name in FACT_CLASSES}
    seen: dict[str, set[str]] = {name: set() for name in FACT_CLASSES}
    for run in runs:
        for fact in run.facts:
            label = classify_fact(fact.text)
            key = _one_line(fact.text)
            if key in seen[label]:
                continue
            seen[label].add(key)
            samples[label].append(fact.text)
    for label in FACT_CLASSES:
        print(f"\n-- {label} samples --")
        for text in samples[label][:wanted]:
            print(f"  * {_one_line(text)}")


def reconcile_legacy(records: list[FactRecord]) -> dict[str, dict[str, list[str]]]:
    """Apply v2 and v1 to the same facts and bucket every disagreement.

    ``moved`` is keyed by the *v2* class: a fact v1 called executable that v2
    did not.  ``gained`` is keyed by the *v1* class: a fact v1 rejected that v2
    accepts.  The key is the label a reader needs in order to see which rule
    changed the count.
    """

    moved: dict[str, list[str]] = {}
    gained: dict[str, list[str]] = {}
    for fact in records:
        new_class = classify_fact(fact.text)
        was_executable = is_executable_fact_legacy(fact.text)
        if was_executable and new_class != "executable":
            moved.setdefault(new_class, []).append(fact.text)
        elif not was_executable and new_class == "executable":
            gained.setdefault(classify_fact_legacy(fact.text), []).append(fact.text)
    return {"moved": moved, "gained": gained}


def print_reconciliation(records: list[FactRecord], examples: int) -> None:
    groups = reconcile_legacy(records)
    moved = groups["moved"]
    gained = groups["gained"]
    moved_total = sum(len(texts) for texts in moved.values())
    gained_total = sum(len(texts) for texts in gained.values())

    print(
        f"\n== reconciliation: {EXECUTABLE_DEFINITION_VERSION} vs "
        f"{LEGACY_EXECUTABLE_DEFINITION_VERSION} on the same {len(records)} facts =="
    )
    print(
        f"{EXECUTABLE_DEFINITION_VERSION} executable={sum(1 for f in records if classify_fact(f.text) == 'executable')}, "
        f"{LEGACY_EXECUTABLE_DEFINITION_VERSION} executable={sum(1 for f in records if is_executable_fact_legacy(f.text))}, "
        f"moved={moved_total}, gained={gained_total}"
    )

    if moved_total:
        parts = ", ".join(
            f"{label} {len(texts)}" for label, texts in sorted(moved.items(), key=lambda kv: -len(kv[1]))
        )
        print(f"\n{moved_total} facts moved out of executable: {parts}")
    else:
        print("\n0 facts moved out of executable")

    for label, texts in sorted(moved.items(), key=lambda kv: -len(kv[1])):
        print(f"  [{label}] {len(texts)} facts ({_pct(len(texts), moved_total)} of moved)")
        seen: set[str] = set()
        shown = 0
        for text in texts:
            snippet = _one_line(text, RECONCILE_CHARS)
            if snippet in seen:
                continue
            seen.add(snippet)
            print(f"    - [{label}] {snippet}")
            shown += 1
            if examples and shown >= examples:
                break

    if gained_total:
        parts = ", ".join(
            f"{label} {len(texts)}" for label, texts in sorted(gained.items(), key=lambda kv: -len(kv[1]))
        )
        print(f"\n{gained_total} facts gained as executable: {parts}")
        for label, texts in sorted(gained.items(), key=lambda kv: -len(kv[1])):
            print(f"  [{label} -> executable] {len(texts)} facts")
            seen = set()
            shown = 0
            for text in texts:
                snippet = _one_line(text, RECONCILE_CHARS)
                if snippet in seen:
                    continue
                seen.add(snippet)
                print(f"    - [executable] {snippet}")
                shown += 1
                if examples and shown >= examples:
                    break
    else:
        print("\n0 facts gained as executable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="process at most N runs (0 = all); numerator and denominator stay on the same population",
    )
    parser.add_argument("--verbose", action="store_true", help="print distinct samples per class")
    parser.add_argument(
        "--transport",
        choices=("tcp", "docker"),
        default="tcp",
        help="MySQL via 127.0.0.1:3307 (default) or docker exec",
    )
    parser.add_argument("--transitions", default="", help="explicit transitions jsonl")
    parser.add_argument(
        "--reconcile-legacy",
        action="store_true",
        help="apply v2 and v1 to the same facts and print every disagreement group",
    )
    parser.add_argument(
        "--reconcile-examples",
        type=int,
        default=RECONCILE_EXAMPLES,
        help="examples per disagreement group (default 10; 0 = every disagreeing fact)",
    )
    args = parser.parse_args()

    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")
    if args.reconcile_examples < 0:
        raise SystemExit("--reconcile-examples must be >= 0")

    path = Path(args.transitions) if args.transitions else newest_transitions()
    if not path.is_file():
        raise SystemExit(f"transitions file not found: {path}")

    runs = read_transitions(path, args.limit)
    if not runs:
        raise SystemExit(f"no run rows parsed from {path}")

    commands = sum(run.commands for run in runs)
    by_run = fetch_facts((run.run_id for run in runs), args.transport)
    last_commands = fetch_last_commands((run.run_id for run in runs), args.transport)
    for run in runs:
        run.facts = by_run.get(run.run_id, [])
        run.last_command_at = last_commands.get(run.run_id, "")

    all_facts = [fact for run in runs for fact in run.facts]
    lifetime_facts = [fact for run in runs for fact in run.lifetime_facts()]
    after_last = [
        fact
        for run in runs
        for fact in run.facts
        if run.last_command_at and fact.created_at > run.last_command_at
    ]

    today = window_stats(all_facts, commands)
    lifetime = window_stats(lifetime_facts, commands)
    after = window_stats(after_last, commands)

    export_facts = sum(run.export_facts for run in runs)
    verified_total = sum(1 for fact in all_facts if fact.verified)

    print("== source ==")
    relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    print(f"transitions : {relative}")
    print(f"              {len(runs)} runs, {commands} commands")
    print(
        f"facts       : mysql {MYSQL_HOST}:{MYSQL_PORT} run_events "
        "muteki.fact_added payload.payload.fact, same run ids"
    )
    print(f"verified    : {verified_total} / {len(all_facts)} ({_pct(verified_total, len(all_facts))})")
    print(
        f"definition  : executable = {EXECUTABLE_DEFINITION_VERSION} (strict), "
        f"legacy = {LEGACY_EXECUTABLE_DEFINITION_VERSION} (frozen, reported alongside only)"
    )

    print("\n== LIMITATION: the two sides of the ratio are not one snapshot ==")
    print(f"- commands and final_status come from the transition export ({path.name}).")
    print(f"- fact text comes from the current MySQL run_events, not from that file.")
    print(
        f"- the export's own fact counter (sum of max state.facts) is {export_facts}; "
        f"the current database holds {len(all_facts)} fact rows for the same runs "
        f"(difference {len(all_facts) - export_facts})."
    )
    print(
        f"- {len(after_last)} facts ({_pct(len(after_last), len(all_facts))}) are timestamped "
        "after the run's last command; they were published in the finalisation batch."
    )
    print(
        "- therefore facts_as_of_today is a run-output rate normalised by commands, "
        "not an increment attributable to the Nth command."
    )
    print(
        "- only facts_in_lifetime can be read as \"facts produced per command\"; "
        "the after-last-command rows have no command to divide by."
    )

    print("\n== windows ==")
    print(
        f"{'window':<22}{'facts':>7}"
        f"{'exec v2':>9}{'v2 share':>10}{'v2/100':>8}"
        f"{'exec v1':>9}{'v1 share':>10}{'v1/100':>8}"
    )
    for name, stats in (
        ("facts_as_of_today", today),
        ("facts_in_lifetime", lifetime),
    ):
        print(
            f"{name:<22}{stats['facts']:>7}"
            f"{stats['executable']:>9}{stats['ratio'] * 100:>9.1f}%"
            f"{stats['executable_per_100_commands']:>8.2f}"
            f"{stats['executable_legacy']:>9}{stats['legacy_ratio'] * 100:>9.1f}%"
            f"{stats['legacy_per_100_commands']:>8.2f}"
        )
    print(
        f"{'after_last_command':<22}{after['facts']:>7}"
        f"{after['executable']:>9}{after['ratio'] * 100:>9.1f}%{'n/a':>8}"
        f"{after['executable_legacy']:>9}{after['legacy_ratio'] * 100:>9.1f}%{'n/a':>8}"
    )

    print(f"\n== overall (facts_as_of_today, n={len(all_facts)}) ==")
    print(f"definition version           {EXECUTABLE_DEFINITION_VERSION} (headline)")
    label_v2 = f"executable ({EXECUTABLE_DEFINITION_VERSION})"
    label_v1 = f"executable ({LEGACY_EXECUTABLE_DEFINITION_VERSION}, legacy)"
    print(f"{label_v2:<29}{today['executable']:>4}  {today['ratio'] * 100:>5.1f}%")
    print(
        f"{label_v1:<29}{today['executable_legacy']:>4}  {today['legacy_ratio'] * 100:>5.1f}%"
        "   (frozen rule, not the headline)"
    )
    print(f"moved out of executable      {today['moved_from_legacy']}")
    print(f"facts / 100 commands         {_rate(len(all_facts), commands):.2f}")
    print(f"executable / 100 commands    {today['executable_per_100_commands']:.2f}  ({EXECUTABLE_DEFINITION_VERSION})")
    print(
        f"executable / 100 commands    {today['legacy_per_100_commands']:.2f}  "
        f"({LEGACY_EXECUTABLE_DEFINITION_VERSION}, legacy)"
    )
    print(f"\n{'class':<12}{'count':>8}{'share':>9}")
    for label in FACT_CLASSES:
        print(f"{label:<12}{today['by_class'][label]:>8}{_pct(today['by_class'][label], len(all_facts)):>9}")

    print("\n== per status (facts_as_of_today; exact match) ==")
    print(
        f"{'status':<22}{'runs':>6}{'commands':>10}{'facts':>8}"
        f"{'exec v2':>9}{'v2/100':>8}{'life v2/100':>13}"
        f"{'exec v1':>9}{'v1/100':>8}"
    )
    status_rows: dict[str, dict[str, int]] = {}
    for run in runs:
        bucket = status_rows.setdefault(
            run.status,
            {
                "runs": 0,
                "commands": 0,
                "facts": 0,
                "executable": 0,
                "life_exec": 0,
                "executable_legacy": 0,
            },
        )
        bucket["runs"] += 1
        bucket["commands"] += run.commands
        bucket["facts"] += len(run.facts)
        bucket["executable"] += sum(
            1 for fact in run.facts if classify_fact(fact.text) == "executable"
        )
        bucket["life_exec"] += sum(
            1 for fact in run.lifetime_facts() if classify_fact(fact.text) == "executable"
        )
        bucket["executable_legacy"] += sum(
            1 for fact in run.facts if is_executable_fact_legacy(fact.text)
        )

    for status, bucket in status_rows.items():
        print(
            f"{status:<22}{bucket['runs']:>6}{bucket['commands']:>10}{bucket['facts']:>8}"
            f"{bucket['executable']:>9}{_rate(bucket['executable'], bucket['commands']):>8.2f}"
            f"{_rate(bucket['life_exec'], bucket['commands']):>13.2f}"
            f"{bucket['executable_legacy']:>9}"
            f"{_rate(bucket['executable_legacy'], bucket['commands']):>8.2f}"
        )

    print("\n== per run (facts_as_of_today) ==")
    print(
        f"{'run_id':<38}{'status':<22}{'cmds':>6}{'facts':>7}"
        f"{'exec v2':>8}{'v2 share':>10}{'v2/100':>8}"
    )
    for run in runs:
        executable = sum(1 for fact in run.facts if classify_fact(fact.text) == "executable")
        share = executable / len(run.facts) if run.facts else 0.0
        print(
            f"{run.run_id:<38}{run.status:<22}{run.commands:>6}{len(run.facts):>7}"
            f"{executable:>8}{share * 100:>9.1f}%"
            f"{_rate(executable, run.commands):>8.2f}"
        )

    if args.verbose:
        print_samples(runs)

    if args.reconcile_legacy:
        print_reconciliation(all_facts, args.reconcile_examples)

    record_analysis(
        "measure_executable_facts",
        f"mysql({MYSQL_HOST}:{MYSQL_PORT}) run_events muteki.fact_added "
        f"+ transitions {relative.name} (commands/labels)",
        {
            "runs": len(runs),
            "commands": commands,
            "facts": len(all_facts),
            # Both numbers live under the same key, each wearing its version,
            # so the new ratio cannot be quoted as if it were the old one.
            "executable": {
                "definition_version": EXECUTABLE_DEFINITION_VERSION,
                "count": today["executable"],
                "ratio": today["ratio"],
                "per_100_commands": today["executable_per_100_commands"],
                "legacy_definition_version": LEGACY_EXECUTABLE_DEFINITION_VERSION,
                "legacy_count": today["executable_legacy"],
                "legacy_ratio": today["legacy_ratio"],
                "legacy_per_100_commands": today["legacy_per_100_commands"],
                "moved_from_legacy": today["moved_from_legacy"],
            },
            "facts_per_100_commands": _rate(len(all_facts), commands),
            "class_counts": dict(today["by_class"]),
            "by_status": status_rows,
            "verified_facts": verified_total,
            "export_state_facts": export_facts,
            "windows": {
                "facts_as_of_today": {
                    "facts": today["facts"],
                    "executable": today["executable"],
                    "executable_legacy": today["executable_legacy"],
                    "ratio": today["ratio"],
                    "legacy_ratio": today["legacy_ratio"],
                    "executable_per_100_commands": today["executable_per_100_commands"],
                },
                "facts_in_lifetime": {
                    "facts": lifetime["facts"],
                    "executable": lifetime["executable"],
                    "executable_legacy": lifetime["executable_legacy"],
                    "ratio": lifetime["ratio"],
                    "legacy_ratio": lifetime["legacy_ratio"],
                    "executable_per_100_commands": lifetime["executable_per_100_commands"],
                },
                "facts_after_last_command": {
                    "facts": after["facts"],
                    "executable": after["executable"],
                    "executable_legacy": after["executable_legacy"],
                    "ratio": after["ratio"],
                    "legacy_ratio": after["legacy_ratio"],
                },
            },
        },
        filters={
            "limit_runs": args.limit or None,
            "transport": args.transport,
            "transitions": path.name,
            "definition_version": EXECUTABLE_DEFINITION_VERSION,
            # The guard stores only code_mtime; keep the module path in filters
            # so the snapshot can be traced back without editing the guard.
            "code_path": "backend/app/services/fact_quality.py",
        },
        data_window=f"{len(runs)} runs / {commands} commands",
        code_path=ROOT / "backend" / "app" / "services" / "fact_quality.py",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
