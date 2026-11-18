#!/usr/bin/env python
"""Preview the trajectory compressor and the failure diagnoser on real runs.

Read-only: this script never touches the database, never starts a service, and
never writes into ``data/``.  Everything comes from the exported JSONL plus the
raw CLI logs on disk, which is what makes the numbers in the report
reproducible on a machine with no containers running.

Examples
--------
Compress one run and print the head of the compressed text::

    python scripts/inspect_trajectory.py compress --run 5cb19393-5159-4ad3-b1b7-dea3bb4d2361

Answer the three follow-up questions from the compressed form alone::

    python scripts/inspect_trajectory.py questions --run 5cb19393-...

Diagnose one run::

    python scripts/inspect_trajectory.py diagnose --run 5cb19393-...

Layer distribution over the first ten failed runs (the report's table)::

    python scripts/inspect_trajectory.py sweep --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.training.diagnosis import (  # noqa: E402
    LAYERS,
    diagnose_run,
    layer_distribution,
)
from app.training.trajectory import (  # noqa: E402
    _payload_slices,
    artifact_coverage,
    compress_trajectory,
    extract_artifacts,
    load_run_trajectory,
    load_run_trajectories,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "training"
WORKSPACES = ROOT / "data" / "workspaces"

FAILED_STATUSES = {"COMPLETED_UNSOLVED", "TIMEOUT", "FAILED_ENGINE"}


def _load(run_id: str, *, backfill: bool) -> "object":
    return load_run_trajectory(
        run_id, data_dir=str(DATA), workspace_dir=str(WORKSPACES), backfill=backfill
    )


def cmd_compress(args: argparse.Namespace) -> int:
    trajectory = _load(args.run, backfill=not args.no_backfill)
    compressed = compress_trajectory(trajectory, window=args.window)
    present, missing = artifact_coverage(compressed)
    print(
        f"run={trajectory.run_id} events={len(trajectory)} "
        f"backfilled={trajectory.backfilled_events} window={args.window}"
    )
    print(
        f"chars: source={compressed.source_chars} compressed={compressed.compressed_chars} "
        f"ratio={compressed.ratio:.3f}"
    )
    print(f"artifacts: {len(present)} present / {len(missing)} missing")
    for item in missing[:5]:
        print(f"  MISSING {item}")
    print("-" * 72)
    print(compressed.text[: args.head])
    return 0


def cmd_questions(args: argparse.Namespace) -> int:
    trajectory = _load(args.run, backfill=not args.no_backfill)
    compressed = compress_trajectory(trajectory, window=args.window)
    text = compressed.text

    last = next((e.command.strip() for e in reversed(trajectory.events) if e.command.strip()), "")
    addresses = {
        a
        for event in trajectory.events
        for a in extract_artifacts(event.command + "\n" + event.result)
        if a.startswith("{{target_url}}") or a.startswith("http")
    }
    payloads = {p for event in trajectory.events for p in _payload_slices(event.command)}

    for label, values in (
        ("1) last command", [last]),
        ("2) addresses", sorted(addresses)),
        ("3) payloads", sorted(payloads)),
    ):
        missing = [v for v in values if v not in text]
        print(f"{label}: {len(values)} found, {len(missing)} missing from compressed text")
        for value in sorted(values)[: args.show]:
            print(f"    {value[:160]}")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    trajectory = _load(args.run, backfill=not args.no_backfill)
    diagnosis = diagnose_run(trajectory)
    print(diagnosis.explain())
    return 0


def _failed_run_ids(limit: int) -> list[str]:
    runs_path = sorted(DATA.glob("runs-*.jsonl"))[-1]
    rows = [
        json.loads(line)
        for line in runs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [
        row["run_id"]
        for row in rows
        if row.get("status") in FAILED_STATUSES and int(row.get("command_events") or 0) >= 10
    ]
    return sorted(ids)[:limit]


def cmd_sweep(args: argparse.Namespace) -> int:
    ids = _failed_run_ids(args.limit)
    trajectories = load_run_trajectories(
        ids, data_dir=str(DATA), workspace_dir=str(WORKSPACES), backfill=not args.no_backfill
    )
    diagnoses = [diagnose_run(trajectories[run_id]) for run_id in ids if run_id in trajectories]
    counts = layer_distribution(diagnoses)

    print(f"failed runs sampled: {len(diagnoses)} (deterministic: sorted run id, first {args.limit})")
    print("layer distribution: " + "  ".join(f"{layer}={counts[layer]}" for layer in LAYERS))
    print()
    header = f"{'run':10s} {'layer':5s} {'conf':>5s} {'score':>6s} {'n':>4s} {'facts':>5s}  root_cause"
    print(header)
    print("-" * len(header))
    for diagnosis in diagnoses:
        print(
            f"{diagnosis.run_id[:8]:10s} {diagnosis.layer:5s} {diagnosis.confidence:5.2f} "
            f"{diagnosis.progress_score:6.1f} {diagnosis.features['n']:4d} "
            f"{diagnosis.features['facts_total']:5d}  {diagnosis.root_cause[:110]}"
        )
        if args.verbose and diagnosis.evidence:
            item = diagnosis.evidence[0]
            print(f"{'':10s} evidence: event {item.event_id} ({item.kind}) {item.excerpt[:120]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    compress = sub.add_parser("compress", help="compress one run and print the result")
    compress.add_argument("--run", required=True)
    compress.add_argument("--window", type=int, default=10)
    compress.add_argument("--head", type=int, default=4000)
    compress.add_argument("--no-backfill", action="store_true")
    compress.set_defaults(func=cmd_compress)

    questions = sub.add_parser("questions", help="prove the three follow-up questions still work")
    questions.add_argument("--run", required=True)
    questions.add_argument("--window", type=int, default=10)
    questions.add_argument("--show", type=int, default=5)
    questions.add_argument("--no-backfill", action="store_true")
    questions.set_defaults(func=cmd_questions)

    diagnose = sub.add_parser("diagnose", help="diagnose one run")
    diagnose.add_argument("--run", required=True)
    diagnose.add_argument("--no-backfill", action="store_true")
    diagnose.set_defaults(func=cmd_diagnose)

    sweep = sub.add_parser("sweep", help="diagnose N failed runs and print the layer table")
    sweep.add_argument("--limit", type=int, default=10)
    sweep.add_argument("--verbose", action="store_true")
    sweep.add_argument("--no-backfill", action="store_true")
    sweep.set_defaults(func=cmd_sweep)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
