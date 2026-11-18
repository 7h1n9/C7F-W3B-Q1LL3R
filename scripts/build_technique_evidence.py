"""Rebuild the technique-evidence file from the newest transition export.

The ranking in ``known_exploit.technique_candidates`` reads
``data/knowledge/technique-evidence.json``.  That file must be *derived*, never
hand-edited: an evidence file someone typed is an opinion, and the whole point
of this layer is that the numbers came from measured runs.

Regenerate after a batch of solves:

    python scripts/build_technique_evidence.py --dry-run
    python scripts/build_technique_evidence.py
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.training.candidates import technique_labels  # noqa: E402
from app.training.technique_evidence import (  # noqa: E402
    apply_evidence_boost,
    candidate_evidence,
    write_evidence,
)

TRAINING_DIR = ROOT / "data" / "training"


def newest(pattern: str) -> Path | None:
    files = sorted(TRAINING_DIR.glob(pattern))
    return files[-1] if files else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transitions", default="")
    parser.add_argument("--runs", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    transitions_path = Path(args.transitions) if args.transitions else newest("transitions-*.jsonl")
    runs_path = Path(args.runs) if args.runs else newest("runs-*.jsonl")
    if not transitions_path or not transitions_path.is_file():
        raise SystemExit("no transitions export found; run export_training_transitions.py first")
    if not runs_path or not runs_path.is_file():
        raise SystemExit("no runs sidecar found; the split labels live there")

    audits = {}
    with runs_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            audits[row.get("run_id")] = row

    # Only the train split may inform the prior.  Using dev or test here would
    # leak the held-out set into the ranking and make any later A/B meaningless.
    train_accepted = {
        run_id
        for run_id, audit in audits.items()
        if audit.get("split") == "train"
        and audit.get("accepted")
        and not audit.get("exclude_reason")
    }

    fact_runs: dict[str, set[str]] = collections.defaultdict(set)
    dead_runs: dict[str, set[str]] = collections.defaultdict(set)
    with transitions_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                transition = json.loads(line)
            except ValueError:
                continue
            command = (transition.get("action") or {}).get("command") or ""
            if not command:
                continue
            labels = technique_labels(command)
            if not labels:
                continue
            run_id = str(transition.get("run_id") or "")
            observation = transition.get("observation") or {}
            for label in labels:
                if int(observation.get("delta_facts") or 0) > 0 and run_id in train_accepted:
                    fact_runs[label].add(run_id)
                if int(observation.get("delta_dead_ends") or 0) > 0:
                    dead_runs[label].add(run_id)

    techniques = {
        label: {"runs_fact": len(fact_runs[label]), "runs_dead": len(dead_runs[label])}
        for label in set(fact_runs) | set(dead_runs)
    }

    merged = candidate_evidence(techniques)
    print(f"source: {transitions_path.name} / {runs_path.name}")
    print(f"train accepted runs: {len(train_accepted)}")
    print(f"\n{'candidate':<44} {'fact':>5} {'dead':>5} {'rate':>6}  promoted")
    for name, row in sorted(merged.items(), key=lambda kv: -kv[1]["runs"]):
        promoted = row["runs"] >= 4 and row["rate"] >= 0.30
        print(
            f"{name:<44} {row['runs_fact']:>5} {row['runs_dead']:>5} "
            f"{row['rate']:>5.0%}  {'yes' if promoted else '-'}"
        )

    if args.dry_run:
        return 0
    target = write_evidence(techniques)
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
