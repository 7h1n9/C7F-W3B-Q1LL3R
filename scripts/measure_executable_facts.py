"""Measure executable facts per run and per final status.

Read-only.  Command counts and run labels come from the newest
``data/training/transitions-*.jsonl`` (one line per command); fact text comes
from MySQL ``run_events`` (``event_type = 'muteki.fact_added'``), restricted to
the same run ids.  The transition export keeps only fact *counts*.

Two windows are printed because the numerator and denominator are not from the
same snapshot:

* ``facts_as_of_today`` -- every fact row the current database holds for these
  runs.  This is the headline, comparable to the project's historical "N facts
  per 100 commands".
* ``facts_in_lifetime`` -- only facts whose timestamp is at or before the run's
  last ``muteki.worker_command``.  This is the only window that can be read as
  "facts produced per command"; the remainder was published in the finalisation
  batch after the trajectory ended.

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
    DETAIL_CLASSES,
    FACT_CLASSES,
    classify_fact,
    executable_fact_summary,
)

MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = "3307"
MYSQL_USER = "ctf_agent"
MYSQL_PASSWORD = "ctf_agent"
MYSQL_DB = "ctf_agent"
DOCKER_CONTAINER = "c7f-w3b-q1ll3r-mysql-1"

RUN_CHUNK = 25  # long IN lists have been SIGTERM'd on this box
SAMPLE_CHARS = 300


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


def _one_line(text: str) -> str:
    return " ".join(text.split())[:SAMPLE_CHARS]


def _rate(executable: int, commands: int) -> float:
    return round(executable * 100 / commands, 2) if commands else 0.0


def window_stats(records: list[FactRecord], commands: int) -> dict[str, Any]:
    summary = executable_fact_summary([fact.text for fact in records])
    counts = summary["counts"]
    assert isinstance(counts, dict)
    return {
        "facts": len(records),
        "executable": counts["executable"],
        "ratio": summary["shares"]["executable"],
        "executable_per_100_commands": _rate(counts["executable"], commands),
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
    args = parser.parse_args()

    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")

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
    today_summary = today["summary"]
    assert isinstance(today_summary, dict)
    today_counts = today_summary["counts"]
    assert isinstance(today_counts, dict)
    by_detail = today_summary["by_detail"]
    assert isinstance(by_detail, dict)

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
    print(f"{'window':<22}{'facts':>7}{'exec':>7}{'share':>8}{'exec/100cmds':>14}")
    for name, stats in (
        ("facts_as_of_today", today),
        ("facts_in_lifetime", lifetime),
    ):
        print(
            f"{name:<22}{stats['facts']:>7}{stats['executable']:>7}"
            f"{stats['ratio'] * 100:>7.1f}%{stats['executable_per_100_commands']:>14.2f}"
        )
    print(
        f"{'after_last_command':<22}{after['facts']:>7}{after['executable']:>7}"
        f"{after['ratio'] * 100:>7.1f}%{'n/a':>14}"
    )

    print("\n== overall (facts_as_of_today) ==")
    print(f"facts                        {len(all_facts)}")
    print(f"executable                   {today_counts['executable']}")
    print(f"executable ratio             {today['ratio'] * 100:.1f}%")
    print(f"facts / 100 commands         {_rate(len(all_facts), commands):.2f}")
    print(f"executable / 100 commands    {today['executable_per_100_commands']:.2f}")
    print(f"{'class':<12}{'count':>8}{'share':>9}")
    for label in FACT_CLASSES:
        print(f"{label:<12}{today_counts[label]:>8}{_pct(today_counts[label], len(all_facts)):>9}")

    print("\n== by detail (additional field, not a replacement) ==")
    print(f"{'detail':<12}{'count':>8}{'share':>9}")
    for label in DETAIL_CLASSES:
        print(f"{label:<12}{by_detail[label]:>8}{_pct(by_detail[label], len(all_facts)):>9}")

    print("\n== per status (facts_as_of_today; exact match) ==")
    print(
        f"{'status':<22}{'runs':>6}{'commands':>10}{'facts':>8}{'exec':>7}"
        f"{'share':>8}{'exec/100':>10}{'life/100':>10}"
    )
    status_rows: dict[str, dict[str, int]] = {}
    for run in runs:
        bucket = status_rows.setdefault(
            run.status,
            {"runs": 0, "commands": 0, "facts": 0, "executable": 0, "life_exec": 0},
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

    for status, bucket in status_rows.items():
        print(
            f"{status:<22}{bucket['runs']:>6}{bucket['commands']:>10}"
            f"{bucket['facts']:>8}{bucket['executable']:>7}"
            f"{_pct(bucket['executable'], bucket['facts']):>8}"
            f"{_rate(bucket['executable'], bucket['commands']):>10.2f}"
            f"{_rate(bucket['life_exec'], bucket['commands']):>10.2f}"
        )

    print("\n== per run (facts_as_of_today) ==")
    print(
        f"{'run_id':<38}{'status':<22}{'cmds':>6}{'facts':>7}{'exec':>6}"
        f"{'share':>8}{'exec/100':>10}"
    )
    for run in runs:
        executable = sum(1 for fact in run.facts if classify_fact(fact.text) == "executable")
        share = executable / len(run.facts) if run.facts else 0.0
        print(
            f"{run.run_id:<38}{run.status:<22}{run.commands:>6}{len(run.facts):>7}"
            f"{executable:>6}{share * 100:>7.1f}%"
            f"{_rate(executable, run.commands):>10.2f}"
        )

    if args.verbose:
        print_samples(runs)

    record_analysis(
        "measure_executable_facts",
        f"mysql({MYSQL_HOST}:{MYSQL_PORT}) run_events muteki.fact_added "
        f"+ transitions {relative.name} (commands/labels)",
        {
            "runs": len(runs),
            "commands": commands,
            "facts": len(all_facts),
            "executable": today_counts["executable"],
            "ratio": today["ratio"],
            "facts_per_100_commands": _rate(len(all_facts), commands),
            "executable_per_100_commands": today["executable_per_100_commands"],
            "class_counts": dict(today_counts),
            "by_detail": dict(by_detail),
            "by_status": status_rows,
            "verified_facts": verified_total,
            "export_state_facts": export_facts,
            "windows": {
                "facts_as_of_today": {
                    "facts": today["facts"],
                    "executable": today["executable"],
                    "ratio": today["ratio"],
                    "executable_per_100_commands": today["executable_per_100_commands"],
                },
                "facts_in_lifetime": {
                    "facts": lifetime["facts"],
                    "executable": lifetime["executable"],
                    "ratio": lifetime["ratio"],
                    "executable_per_100_commands": lifetime["executable_per_100_commands"],
                },
                "facts_after_last_command": {
                    "facts": after["facts"],
                    "executable": after["executable"],
                    "ratio": after["ratio"],
                },
            },
        },
        filters={
            "limit_runs": args.limit or None,
            "transport": args.transport,
            "transitions": path.name,
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
