#!/usr/bin/env python
"""Run the offline scaffold evolution over the archived failure corpus.

Read-only with respect to the running system: no service is started, no
container is touched, no model is called, no rollout happens.  The only write
is ``data/training/variants/<seed policy version>/...``, which is gitignored
because variants are generated artifacts.

Examples
--------
Three generations, four variants each, default corpus (every failed run with at
least ten command events)::

    python scripts/run_evolution.py --generations 3 --population 4

Same, but small enough to eyeball::

    python scripts/run_evolution.py --limit 12 --generations 2 --population 2

The similarity baseline the report compares against (only the focus layer is
mutated, every other layer is carried over byte-identical)::

    python scripts/run_evolution.py --fill 0 --limit 40
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.training.diagnosis import (  # noqa: E402
    LAYERS,
    diagnose_run,
)
from app.training.evolve import (  # noqa: E402
    EVOLUTION_LIMITS,
    MIN_PARENT_SIMILARITY,
    SEED_LAYERS,
    added_rules_text,
    cosine,
    dumps,
    evolve,
    layer_distribution,
    render_layer,
)
from app.training.trajectory import load_run_trajectories  # noqa: E402

FAILED_STATUSES = {"COMPLETED_UNSOLVED", "TIMEOUT", "FAILED_ENGINE"}
MIN_COMMAND_EVENTS = 10


def _latest_runs(data_dir: Path) -> Path:
    candidates = sorted(data_dir.glob("runs-*.jsonl"))
    if not candidates:
        raise SystemExit(f"no runs-*.jsonl under {data_dir}")
    return candidates[-1]


def _failed_run_ids(path: Path, limit: int) -> list[str]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [
        row["run_id"]
        for row in rows
        if row.get("status") in FAILED_STATUSES
        and int(row.get("command_events") or 0) >= MIN_COMMAND_EVENTS
    ]
    return sorted(ids)[:limit] if limit > 0 else sorted(ids)


def _fmt_summary(label: str, summary: dict[str, float]) -> str:
    return (
        f"{label:<6} min={summary['min']:.3f}  median={summary['median']:.3f}  "
        f"max={summary['max']:.3f}  pairs={int(summary['n_pairs'])}"
    )


def _print_seed() -> None:
    print("== seed scaffold ==")
    total = 0
    for layer in LAYERS:
        text = render_layer(SEED_LAYERS[layer])
        total += len(text)
        print(f"  {layer}: {len(SEED_LAYERS[layer].rules):2d} rules, {len(text):5d} chars")
    print(f"  total: {total} chars")


def _write(result: Any, out_root: Path) -> Path:
    target = out_root / result.seed_policy_version
    target.mkdir(parents=True, exist_ok=True)
    seed = {
        "seed_policy_version": result.seed_policy_version,
        "corpus_digest": result.corpus_digest,
        "diagnoses": len(result.diagnoses),
        "layer_distribution": layer_distribution(result.diagnoses),
        "layers": {
            layer: {
                "text": render_layer(SEED_LAYERS[layer]),
                "rules": [
                    {
                        "rule_id": rule.rule_id,
                        "text": rule.text,
                        "tags": list(rule.tags),
                        "group": rule.group,
                        "origin": rule.origin,
                    }
                    for rule in SEED_LAYERS[layer].rules
                ],
            }
            for layer in LAYERS
        },
        "limits": EVOLUTION_LIMITS,
    }
    (target / "seed.json").write_text(
        json.dumps(seed, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    for generation in result.generations:
        directory = target / f"gen{generation.generation}"
        directory.mkdir(parents=True, exist_ok=True)
        for variant in generation.variants:
            path = directory / f"{variant.variant_id}.json"
            path.write_text(
                json.dumps(variant.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
    (target / "evolution.json").write_text(dumps(result), encoding="utf-8")
    return target


def _print_generation(generation: Any) -> None:
    print(f"\n-- gen{generation.generation}: {len(generation.variants)} variant(s) --")
    header = (
        f"{'variant':10s} {'parent':10s} {'layers':12s} {'rules':>5s} "
        f"{'fit':>6s} {'prog':>6s} {'evid':>4s} {'sib_add':>7s}  operators"
    )
    print(header)
    print("-" * len(header))
    added = {variant.variant_id: added_rules_text(variant.layers) for variant in generation.variants}
    for variant in generation.variants:
        layers = ",".join(variant.mutated_layers)
        rules = sum(len(variant.layers[layer].rules) for layer in LAYERS)
        applied = ",".join(op.operator for op in variant.op_log if op.applied)
        # Closest sibling on added rules only: the number that says whether this
        # "accepted" variant is a distinct hypothesis or a rewording of another.
        siblings = [
            cosine(added[variant.variant_id], text)
            for other, text in added.items()
            if other != variant.variant_id
        ]
        sibling_value = f"{min(siblings):7.3f}" if siblings else f"{'-':>7s}"
        print(
            f"{variant.variant_id:10s} {str(variant.parent_id or '-'):10s} {layers:12s} "
            f"{rules:5d} {variant.fitness.score:6.3f} {variant.fitness.mean_progress_score:6.1f} "
            f"{len(variant.evidence_run_ids):4d} {sibling_value}  {applied}"
        )
    for rejection in generation.rejected:
        print(
            f"  REJECTED {rejection.variant_id}: parent {rejection.parent_id} "
            f"similarity {rejection.similarity:.3f} < floor {rejection.floor}"
        )
    similarity = generation.similarity
    print("  similarity " + _fmt_summary("all", similarity["scaffold"]))
    for layer in LAYERS:
        print("             " + _fmt_summary(layer, similarity["per_layer"][layer]))
    diversity = generation.diversity
    added_summary = diversity["pairwise_added_rule_cosine"]
    print(
        f"  diversity  seed_chars={diversity['seed_chars']} "
        f"min_full={diversity['min_full_cosine']:.3f} "
        f"seed_overlap={diversity['seed_overlap']['min']:.3f}"
        f"-{diversity['seed_overlap']['max']:.3f} "
        f"min_added={diversity['min_added_rule_cosine']:.3f} "
        f"added(min/med/max)={added_summary['min']:.3f}/"
        f"{added_summary['median']:.3f}/{added_summary['max']:.3f}"
    )
    print(f"  VERDICT: {diversity['verdict']}")
    activation = {layer: 0 for layer in LAYERS}
    for variant in generation.variants:
        for layer in variant.mutated_layers:
            activation[layer] += 1
    print("  layer activation: " + "  ".join(f"{layer}={activation[layer]}" for layer in LAYERS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--population", type=int, default=4)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="diagnose at most N failed runs (0 = all); sorted by run id for determinism",
    )
    parser.add_argument("--per-op", type=int, default=3, help="diagnoses fed to each focus operator")
    parser.add_argument(
        "--fill",
        type=int,
        default=1,
        help="diagnoses fed to the per-layer top-up; 0 reproduces the one-layer-only baseline",
    )
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "training"))
    parser.add_argument("--workspace-dir", default=str(ROOT / "data" / "workspaces"))
    parser.add_argument(
        "--min-parent-similarity",
        type=float,
        default=MIN_PARENT_SIMILARITY,
        help="reject a candidate whose cosine to its parent is below this floor",
    )
    parser.add_argument("--out", default=str(ROOT / "data" / "training" / "variants"))
    parser.add_argument("--no-write", action="store_true", help="print only, write nothing")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    runs_path = _latest_runs(data_dir)
    run_ids = _failed_run_ids(runs_path, args.limit)
    print(f"runs file: {runs_path.name}")
    print(f"failed runs with >= {MIN_COMMAND_EVENTS} command events: {len(run_ids)}")

    trajectories = load_run_trajectories(
        run_ids,
        data_dir=str(data_dir),
        workspace_dir=args.workspace_dir,
        backfill=False,
    )
    missing = [run_id for run_id in run_ids if run_id not in trajectories]
    diagnoses = [
        diagnose_run(trajectories[run_id]) for run_id in sorted(trajectories)
    ]
    # A confidence of zero means no rule fired: the run is not evidence of any
    # layer's failure, so it must not be used to mutate a layer.
    usable = [diagnosis for diagnosis in diagnoses if diagnosis.confidence > 0]
    print(f"trajectories loaded: {len(trajectories)} (missing: {len(missing)})")
    print(f"diagnoses with a rule hit: {len(usable)} / {len(diagnoses)}")
    counts = layer_distribution(usable)
    print("layer distribution: " + "  ".join(f"{layer}={counts[layer]}" for layer in LAYERS))

    _print_seed()
    result = evolve(
        usable,
        generations=args.generations,
        population=args.population,
        diagnoses_per_op=args.per_op,
        fill_per_layer=args.fill,
        parent_similarity_floor=args.min_parent_similarity,
    )
    print(f"\nseed policy_version: {result.seed_policy_version}")
    print(f"corpus digest: {result.corpus_digest[:12]}  (n={len(result.diagnoses)})")

    for generation in result.generations:
        _print_generation(generation)

    if args.no_write:
        return 0
    target = _write(result, Path(args.out))
    print(f"\nwritten: {target}")
    print(
        "NOTE: variants are generated artifacts and gitignored. "
        "They are NOT ship-ready: nothing here observed a solve."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
