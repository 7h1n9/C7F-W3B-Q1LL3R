"""Turn a transition export into staged policy candidates.

Offline, side-effect free with respect to the running system: it reads
``data/training/transitions-*.jsonl`` and writes
``data/knowledge/policy-candidates/candidates-<stamp>.json``.  Nothing here
touches the database, the arena, or the live knowledge corpus -- publishing is
a separate, gated step.

Usage::

    python scripts/build_policy_candidates.py            # newest export
    python scripts/build_policy_candidates.py --input data/training/transitions-<stamp>.jsonl
    python scripts/build_policy_candidates.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.training.candidates import build_candidate_payload  # noqa: E402
from app.training.policy import compute_policy_version  # noqa: E402

TRAINING_DIR = ROOT / "data" / "training"
OUTPUT_DIR = ROOT / "data" / "knowledge" / "policy-candidates"


def newest_export() -> Path | None:
    files = sorted(TRAINING_DIR.glob("transitions-*.jsonl"))
    return files[-1] if files else None


def load_transitions(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", default="", help="transitions jsonl (default: newest in data/training)")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = Path(args.input) if args.input else newest_export()
    if source is None or not source.is_file():
        raise SystemExit("no transitions export found; run export_training_transitions.py first")

    transitions = load_transitions(source)
    payload = build_candidate_payload(
        transitions,
        policy_version=compute_policy_version()["policy_version"],
        source_file=str(source.relative_to(ROOT)),
    )
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()

    print(json.dumps(payload["stats"], indent=2, ensure_ascii=False))
    print(f"policy_version: {payload['policy_version']}")
    if args.dry_run:
        return 0

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output_dir / f"candidates-{stamp}.json"
    tmp = target.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    tmp.replace(target)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
