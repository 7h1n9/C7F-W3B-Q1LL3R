#!/usr/bin/env python3
"""Run a recoverable off/on knowledge augmentation benchmark experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"


def _ensure_backend_import_path() -> None:
    """Make the script work from both the repository root and backend/."""

    for candidate in (BACKEND_ROOT, REPO_ROOT):
        value = str(candidate)
        if value not in sys.path:
            sys.path.insert(0, value)


_ensure_backend_import_path()

from app.benchmark.experiment import (  # noqa: E402
    DEFAULT_CASE_DIR,
    ExperimentError,
    ExperimentRunner,
    RealRunExecutor,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run benchmark cases across fixed off/on arms with resumable, "
            "immutable output artifacts."
        )
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
        help=f"Directory containing benchmark case JSON files (default: {DEFAULT_CASE_DIR}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Experiment output directory for manifest.json, plan.json, and artifacts/.",
    )
    parser.add_argument(
        "--arms",
        default="off,on",
        help="Comma-separated fixed arms (default: off,on).",
    )
    parser.add_argument(
        "--repeats",
        type=_positive_int,
        default=1,
        help="Number of repeat runs per case and arm (default: 1).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse complete artifacts and execute only missing work.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write only manifest.json and plan.json; do not create SolveRuns.",
    )
    parser.add_argument(
        "--model-config-id",
        help="Explicit enabled Worker model config ID. Defaults to enabled auto-selection.",
    )
    parser.add_argument(
        "--reason-model-config-id",
        help="Explicit openai_compatible Coordinator reasoning model config ID.",
    )
    parser.add_argument("--solver-mode", default="muteki", help="SolveRun solver mode.")
    parser.add_argument(
        "--max-agent-steps",
        type=_positive_int,
        default=120,
        help="Per-Run agent-step limit.",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=_positive_int,
        default=120,
        help="Per-Run tool-call limit.",
    )
    parser.add_argument(
        "--max-context-observations",
        type=_positive_int,
        default=8,
        help="Per-Run context observation limit.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=_positive_int,
        default=900,
        help="Per-attempt runtime limit in seconds.",
    )
    parser.add_argument(
        "--max-total-runtime-seconds",
        type=_positive_int,
        default=3600,
        help="Total Run runtime limit in seconds.",
    )
    return parser


def _execution_error_count(summary: dict[str, Any]) -> int:
    by_arm = summary.get("by_arm")
    if not isinstance(by_arm, dict):
        return 0
    return sum(
        int(item.get("errors") or 0)
        for item in by_arm.values()
        if isinstance(item, dict)
    )


def _preflight_status(output_dir: Path) -> str | None:
    path = output_dir / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        runtime = payload.get("runtime")
        executor = runtime.get("executor") if isinstance(runtime, dict) else None
        preflight = executor.get("preflight") if isinstance(executor, dict) else None
        if isinstance(preflight, dict):
            status = preflight.get("status")
            return str(status) if status is not None else None
    except (OSError, ValueError, TypeError):
        return None
    return None


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    output_dir = args.output_dir.expanduser().resolve()
    executor = RealRunExecutor(
        model_config_id=args.model_config_id,
        reason_model_config_id=args.reason_model_config_id,
        solver_mode=args.solver_mode,
        max_agent_steps=args.max_agent_steps,
        max_tool_calls=args.max_tool_calls,
        max_context_observations=args.max_context_observations,
        max_runtime_seconds=args.max_runtime_seconds,
        max_total_runtime_seconds=args.max_total_runtime_seconds,
    )
    runner = ExperimentRunner(
        case_dir=args.case_dir,
        output_dir=output_dir,
        executor=executor,
        arms=args.arms,
        repeats=args.repeats,
        resume=args.resume,
        dry_run=args.dry_run,
        executor_name="real_run_supervisor",
        runtime_selection=executor.runtime_selection(),
    )
    return await runner.run(), output_dir


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary, output_dir = asyncio.run(_run(args))
    except ExperimentError as error:
        print(f"experiment error: {error}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError) as error:
        print(f"configuration error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    status = _preflight_status(output_dir)
    if status == "error":
        print(
            "executor preflight recorded an error in manifest.json",
            file=sys.stderr,
        )
    if not args.dry_run and _execution_error_count(summary):
        print(
            f"{_execution_error_count(summary)} experiment attempt(s) recorded errors",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
