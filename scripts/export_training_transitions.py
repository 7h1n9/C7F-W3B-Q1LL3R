"""Export run history as (state, action, observation, label) transitions.

Read-only against MySQL, write-only against ``data/training/``.  This is the
data front door of the strategy-layer self-evolution loop: it never mutates a
run, never touches the queue, and can be re-run at any time -- the same inputs
with the same ``--seed`` reproduce byte-identical splits.

Outputs (into ``--output-dir``, default ``data/training/``):

* ``transitions-<stamp>.jsonl`` -- one line per command event, the
  training-ready unit.
* ``runs-<stamp>.jsonl``        -- one line per run, the audit unit: labels,
  counters, redaction totals, and the explicit ``exclude_reason`` when a run
  was dropped.  Nothing is silently filtered.
* ``manifest-<stamp>.json``     -- parameters, counts, split histogram and the
  git revision of the code that produced the export, so a dataset can always
  be traced back to the exact exporter that built it.

Usage::

    python scripts/export_training_transitions.py
    python scripts/export_training_transitions.py --since 2026-10-20 --limit 50
    python scripts/export_training_transitions.py --run-id <uuid> --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.training.transitions import (  # noqa: E402
    EventInput,
    RunInput,
    assign_split,
    build_transitions,
    group_key_for,
)

CONTAINER = "c7f-w3b-q1ll3r-mysql-1"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "training"

# MySQL events arrive in id order within a run, but the export is chunked by
# run id list; keep the chunk small so one oversized run cannot stall the
# pipeline (long multi-statement queries have been SIGTERM'd on this box).
EVENT_CHUNK = 25


def mysql_json(sql: str) -> list[dict]:
    """Run one query and parse it as JSON lines (see build_learned_playbook)."""

    proc = subprocess.run(
        [
            "docker", "exec", CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-uctf_agent", "-pctf_agent", "ctf_agent",
            # ``-r`` (raw) is load-bearing: without it the batch client
            # re-escapes every backslash in the JSON text, turning a stored
            # \" into \\" -- silently corrupting most command payloads.
            "-N", "-B", "-r", "-e", sql,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"mysql failed: {proc.stderr[:400]}")
    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Warning"):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def git_revision() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def fetch_runs(args: argparse.Namespace) -> list[dict]:
    where = ["r.status not in ('CREATED','QUEUED','RUNNING')"]
    if args.since:
        where.append(f"r.created_at >= '{args.since}'")
    if args.run_id:
        where.append(f"r.id = '{args.run_id}'")
    limit = f"limit {int(args.limit)}" if args.limit else ""
    return mysql_json(
        "select json_object("
        "'id', r.id,"
        "'challenge_id', r.challenge_id,"
        "'status', r.status,"
        "'last_error_code', coalesce(r.last_error_code,''),"
        "'last_error_message', coalesce(left(r.last_error_message,400),''),"
        "'created_at', date_format(r.created_at,'%Y-%m-%dT%H:%i:%s'),"
        "'fresh_repro', r.fresh_reproduction_verified,"
        "'challenge_name', coalesce(c.name,'')) "
        "from solve_runs r left join challenges c on c.id = r.challenge_id "
        f"where {' and '.join(where)} order by r.created_at {limit}"
    )


def fetch_accepted_run_ids() -> set[str]:
    """Attribute platform-accepted submissions back to the run that earned them.

    Two channels, unioned:

    * Queue-era runs: ``arena_solve_queue`` carries both ``run_id`` and the
      final ``verdict`` -- a direct foreign key.
    * Pre-queue runs: ``arena_submissions`` has no run link at all, so the
      attribution is challenge name + time window (submission landed while
      the run was live).  Measured 2026-11-04: this matches 121 accepted
      submissions to 121 distinct ``COMPLETED_SOLVED`` runs, one-to-one.
    """

    accepted: set[str] = set()
    for row in mysql_json(
        "select json_object('run_id', run_id) from arena_solve_queue "
        "where verdict = 'accepted' and run_id is not null"
    ):
        if row.get("run_id"):
            accepted.add(row["run_id"])
    for row in mysql_json(
        "select json_object('run_id', r.id) from solve_runs r "
        "join arena_submissions s on s.verdict = 'accepted' "
        "and s.challenge_name = json_unquote(json_extract(r.hints_json,'$.challenge')) "
        "and s.submitted_at between "
        "date_sub(coalesce(r.started_at, r.created_at), interval 10 minute) "
        "and date_add(coalesce(r.finished_at, r.updated_at), interval 15 minute) "
        "where r.status = 'COMPLETED_SOLVED'"
    ):
        if row.get("run_id"):
            accepted.add(row["run_id"])
    return accepted


def fetch_events(run_ids: list[str]) -> dict[str, list[EventInput]]:
    events: dict[str, list[EventInput]] = {rid: [] for rid in run_ids}
    for start in range(0, len(run_ids), EVENT_CHUNK):
        chunk = run_ids[start : start + EVENT_CHUNK]
        id_list = ",".join(f"'{rid}'" for rid in chunk)
        rows = mysql_json(
            "select json_object("
            "'id', e.id,"
            "'run_id', e.run_id,"
            "'event_type', e.event_type,"
            "'payload', coalesce(e.payload_json, json_object()),"
            "'created_at', date_format(e.created_at,'%Y-%m-%dT%H:%i:%s')) "
            "from run_events e "
            f"where e.run_id in ({id_list}) "
            "and e.event_type in ("
            "'muteki.worker_command','muteki.fact_added','muteki.dead_end') "
            # Primary keys are UUIDs: true chronological order comes from
            # created_at, with id as a deterministic tie-break.  The exporter
            # then numbers the rows (``seq``) so the pure core never compares
            # UUID strings.
            "order by e.created_at, e.id"
        )
        seqs: dict[str, int] = {}
        for row in rows:
            rid = row.get("run_id")
            if rid not in events:
                continue
            payload = row.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            seqs[rid] = seqs.get(rid, 0) + 1
            events[rid].append(
                EventInput(
                    seq=seqs[rid],
                    event_id=str(row.get("id") or ""),
                    event_type=row["event_type"],
                    payload=payload if isinstance(payload, dict) else {},
                    created_at=row.get("created_at") or "",
                )
            )
    return events


def fetch_known_secrets() -> tuple[str, ...]:
    """Every flag the platform ever accepted, as literal redaction targets.

    The arena's flag prefixes vary per challenge (``flag{``, ``DASCTF{``,
    ``NSSCTF{``...), so pattern-based redaction alone is not enough -- the
    accepted string itself is the only ground truth.  These never leave this
    process except as the ``{{flag_pattern}}`` placeholder.
    """

    rows = mysql_json(
        "select json_object('flag', attested_flag) from arena_submissions "
        "where verdict = 'accepted' and length(attested_flag) >= 6"
    )
    return tuple(sorted({row["flag"] for row in rows if row.get("flag")}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="", help="only runs created on/after this date")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of runs (newest first is NOT guaranteed; ordering is by created_at)")
    parser.add_argument("--run-id", default="", help="export exactly one run (debugging)")
    parser.add_argument("--seed", default="c7f-v1", help="split seed; same seed reproduces the same split")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    args = parser.parse_args()

    runs = fetch_runs(args)
    print(f"runs fetched: {len(runs)}")
    if not runs:
        return 0

    accepted_ids = fetch_accepted_run_ids()
    print(f"accepted runs: {len(accepted_ids)}")
    known_secrets = fetch_known_secrets()
    print(f"known flags to redact: {len(known_secrets)}")

    run_ids = [row["id"] for row in runs]
    events_by_run = fetch_events(run_ids)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    transitions_out: list[dict] = []
    runs_out: list[dict] = []
    split_hist: Counter[str] = Counter()
    exclude_hist: Counter[str] = Counter()

    for row in runs:
        run = RunInput(
            run_id=row["id"],
            challenge_id=row["challenge_id"],
            status=row["status"],
            last_error_code=row.get("last_error_code") or "",
            last_error_message=row.get("last_error_message") or "",
            created_at=row.get("created_at") or "",
            accepted=row["id"] in accepted_ids,
            fresh_reproduction_verified=bool(row.get("fresh_repro")),
            challenge_name=row.get("challenge_name") or "",
        )
        transitions, audit = build_transitions(
            run, events_by_run.get(run.run_id, []), known_secrets=known_secrets
        )
        group = group_key_for(run.challenge_id, run.challenge_name)
        split = assign_split(group, seed=args.seed)
        audit["group_key"] = group
        audit["split"] = split
        for transition in transitions:
            transition["split"] = split
            transition["group_key"] = group
        transitions_out.extend(transitions)
        runs_out.append(audit)
        if audit["exclude_reason"]:
            exclude_hist[audit["exclude_reason"].split(":", 1)[0]] += 1
        else:
            split_hist[split] += 1

    included = sum(1 for a in runs_out if not a["exclude_reason"])
    manifest = {
        "generated_at": stamp,
        "generator": "scripts/export_training_transitions.py",
        "git_revision": git_revision(),
        "params": {
            "since": args.since or None,
            "limit": args.limit or None,
            "run_id": args.run_id or None,
            "seed": args.seed,
        },
        "runs_total": len(runs_out),
        "runs_included": included,
        "runs_excluded": len(runs_out) - included,
        "exclude_reasons": dict(sorted(exclude_hist.items())),
        "split_histogram": dict(sorted(split_hist.items())),
        "transitions": len(transitions_out),
        "redactions_total": sum(a["redactions"] for a in runs_out),
        # Count only -- the flag values themselves must never reach a manifest.
        "known_secrets_redacted": len(known_secrets),
        "accepted_runs_included": sum(
            1 for a in runs_out if a["accepted"] and not a["exclude_reason"]
        ),
        "fresh_repro_runs_included": sum(
            1
            for a in runs_out
            if a["fresh_reproduction_verified"] and not a["exclude_reason"]
        ),
    }

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    transitions_path = output_dir / f"transitions-{stamp}.jsonl"
    runs_path = output_dir / f"runs-{stamp}.jsonl"
    manifest_path = output_dir / f"manifest-{stamp}.json"

    # Write to a temp sibling first: a half-written dataset must never look
    # complete to a downstream consumer.
    tmp = transitions_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for transition in transitions_out:
            handle.write(json.dumps(transition, ensure_ascii=False) + "\n")
    tmp.replace(transitions_path)

    with runs_path.open("w", encoding="utf-8") as handle:
        for audit in runs_out:
            handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)

    print(f"wrote {transitions_path}")
    print(f"wrote {runs_path}")
    print(f"wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
