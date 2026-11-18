"""Batch benchmark matrix entry point.

``python -m app.benchmark --repeats 10 --randomize-range repeat``

Wires the two pieces that already existed but had no production caller:
``ExperimentRunner`` (manifest / fingerprint / plan / resume / summary) and
``RealRunExecutor`` (a durable SolveRun driven through ``run_supervisor``).
Neither is reimplemented here; this module only decides the inputs and the exit
code, so that a matrix can run unattended from a shell or from CI.

Exit codes: ``0`` finished, ``2`` bad configuration, ``3`` a task failed,
``4`` preflight was not ready (no model / no database — refused before burning
a single Run).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from app.benchmark.experiment import (
    ARMS,
    PLAN_FILENAME,
    SUMMARY_FILENAME,
    ExperimentError,
    ExperimentRunner,
    RealRunExecutor,
    parse_arms,
)
from app.benchmark.range import bring_up

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "backend" / "data" / "experiments"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_TASK_FAILED = 3
EXIT_NOT_READY = 4
# Same code a shell reports for a signalled child, so a wrapper script can tell
# "the operator stopped this matrix" apart from "a task failed".
EXIT_INTERRUPTED = 130

RANDOMIZE_SCOPES = ("never", "run", "repeat")
EXECUTOR_NAME = "real_run_supervisor"


class _RandomizingExecutor:
    """Re-randomise the range when a repetition advances.

    A new port and flag per repetition is the whole point of the runtime
    augmentation (CAP-34): reusing one address across ``--repeats`` would leave
    "memorise the endpoint" open inside a single matrix.  The scope lives here
    rather than inside ``ExperimentRunner`` because the runner must stay
    ignorant of how a target is provisioned.
    """

    def __init__(self, inner: Any, *, scope: str) -> None:
        self._inner = inner
        self._scope = scope
        self._last_repeat: int | None = None

    async def preflight(self) -> Any:
        return await self._inner.preflight()

    async def execute(self, request: Any) -> Any:
        if self._scope == "repeat" and request.repeat != self._last_repeat:
            self._last_repeat = request.repeat
            _randomize(f"repeat {request.repeat}")
        return await self._inner.execute(request)

    def __getattr__(self, name: str) -> Any:
        # manifest_metadata / runtime_selection must keep reaching the real
        # executor, otherwise the recorded manifest would describe the wrapper.
        return getattr(self._inner, name)


def _randomize(context: str) -> None:
    """Rotate the range's host port and flag.

    The flag is a scoring secret: its location is reported for traceability,
    never its value.
    """

    endpoint, _flag = bring_up()
    print(
        f"[matrix] range re-randomised for {context}: {endpoint.base_url}",
        file=sys.stderr,
    )


def _output_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_ROOT / stamp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.benchmark",
        description="Run the case x arm x repeat benchmark matrix.",
    )
    parser.add_argument("--case-dir", default=None, help="case directory (default: app/benchmark/cases)")
    parser.add_argument("--output-dir", default=None, help="artefact directory (default: backend/data/experiments/<utc>)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--arms", default=",".join(ARMS), help=f"comma-separated subset of {','.join(ARMS)}")
    parser.add_argument("--resume", action="store_true", help="reuse completed artefacts instead of failing")
    parser.add_argument("--dry-run", action="store_true", help="write the plan only; never call the model")
    parser.add_argument(
        "--randomize-range",
        choices=RANDOMIZE_SCOPES,
        default="never",
        help="rotate the range port and flag: never | once per matrix | once per repetition",
    )
    parser.add_argument("--worker-model", default=None, help="ModelConfig id for Workers")
    parser.add_argument("--reason-model", default=None, help="ModelConfig id for the Reason pass")
    parser.add_argument("--max-agent-steps", type=int, default=120)
    parser.add_argument("--max-tool-calls", type=int, default=120)
    parser.add_argument(
        "--runtime-seconds",
        type=int,
        default=None,
        help=(
            "Per-Run wall-clock cap.  Defaults to the executor's own limit.  An "
            "unsolved case otherwise burns the full default budget, so a long "
            "matrix is unbounded in wall-clock unless this is pinned."
        ),
    )
    parser.add_argument(
        "--total-runtime-seconds",
        type=int,
        default=None,
        help="Cumulative wall-clock cap for one Run, across attempts.",
    )
    parser.add_argument(
        "--hard-runtime-seconds",
        type=int,
        default=None,
        help=(
            "Executor-side wall clock for one Run; the Run is force-cancelled "
            "when it expires.  Defaults to --runtime-seconds plus a grace "
            "window, so the Run's own deadline normally wins and this only "
            "catches a Run that never terminates."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print only the summary JSON")
    return parser


def _validate(arguments: argparse.Namespace) -> str | None:
    if arguments.repeats < 1:
        return "--repeats must be at least 1"
    try:
        arms = parse_arms(arguments.arms)
    except ValueError as error:
        return f"--arms: {error}"
    if not arms:
        return "--arms must select at least one arm"
    case_dir = Path(arguments.case_dir).expanduser() if arguments.case_dir else DEFAULT_CASES_DIR
    if not case_dir.is_dir():
        return f"case directory does not exist: {case_dir}"
    if not sorted(case_dir.glob("*.json")):
        return f"case directory has no *.json cases: {case_dir}"
    return None


def _report(summary: dict[str, Any], *, output_dir: Path, only_json: bool) -> int:
    by_arm = summary.get("by_arm") or {}
    errors = sum(int((arm or {}).get("errors") or 0) for arm in by_arm.values())
    if only_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary.get("stopped_early"):
            return EXIT_INTERRUPTED
        return EXIT_TASK_FAILED if errors else EXIT_OK

    print(f"[matrix] output    {output_dir}")
    print(f"[matrix] records   {summary.get('record_count', 0)}")
    for arm, stats in sorted(by_arm.items()):
        stats = stats or {}
        rate = stats.get("success_rate")
        process = stats.get("process_metrics") or {}
        repeats = process.get("repeat_attempts") or {}
        misled = process.get("prior_misled") or {}
        print(
            f"[matrix] {arm:>3}  success {stats.get('success', 0)}/{stats.get('attempts', 0)}"
            f"  rate {rate if rate is not None else 'n/a'}"
            f"  repeat {repeats.get('pooled_repeat_ratio')}"
            f"  prior_misled {misled.get('pooled_misled_rate')}"
        )
    paired = summary.get("paired_effect") or {}
    mcnemar = paired.get("mcnemar") if isinstance(paired.get("mcnemar"), dict) else {}
    print(
        f"[matrix] paired    {paired.get('sample_count', 0)} pairs"
        f"  delta {paired.get('success_delta')}"
        f"  ci95 {paired.get('success_delta_bootstrap_ci_95')}"
        f"  p {mcnemar.get('p_value')}"
        f"  claim {paired.get('claim')}"
    )
    skipped = summary.get("skipped_cases") or []
    if skipped:
        print(f"[matrix] skipped   {len(skipped)} case(s) needing a range this repo does not ship")
    print(f"[matrix] summary   {output_dir / SUMMARY_FILENAME}")
    if summary.get("stopped_early"):
        print(
            f"[matrix] stopped   {summary.get('completed_tasks', 0)} of "
            f"{summary.get('planned_tasks', 0)} task(s) attempted; the remainder "
            "were never started",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    if errors:
        print(f"[matrix] {errors} task(s) errored; see the raw artefacts", file=sys.stderr)
    return EXIT_TASK_FAILED if errors else EXIT_OK


async def _run(arguments: argparse.Namespace, stop: threading.Event) -> int:
    output_dir = _output_dir(arguments.output_dir)
    case_dir = Path(arguments.case_dir).expanduser() if arguments.case_dir else DEFAULT_CASES_DIR

    executor: Any = RealRunExecutor(
        model_config_id=arguments.worker_model,
        reason_model_config_id=arguments.reason_model,
        max_agent_steps=arguments.max_agent_steps,
        max_tool_calls=arguments.max_tool_calls,
        # ``None`` keeps the executor's own default: a flag that was never set
        # must not silently shrink an existing manifest's budget.
        max_runtime_seconds=arguments.runtime_seconds,
        max_total_runtime_seconds=arguments.total_runtime_seconds,
        hard_runtime_seconds=arguments.hard_runtime_seconds,
        stop_requested=stop.is_set,
    )
    if arguments.randomize_range == "run":
        _randomize("this matrix")
    elif arguments.randomize_range == "repeat":
        executor = _RandomizingExecutor(executor, scope="repeat")

    if not arguments.dry_run:
        preflight = dict(await executor.preflight())
        if preflight.get("status") != "ready":
            print(
                json.dumps({"status": "NOT_READY", "preflight": preflight}, ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
            return EXIT_NOT_READY
        if not arguments.json:
            print(f"[matrix] preflight ok: {preflight.get('worker_model', {}).get('name')}")

    runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=executor,
        arms=parse_arms(arguments.arms),
        repeats=arguments.repeats,
        resume=arguments.resume,
        dry_run=arguments.dry_run,
        executor_name=EXECUTOR_NAME,
        stop_requested=stop.is_set,
    )
    summary = await runner.run()
    if arguments.dry_run:
        plan_path = output_dir / PLAN_FILENAME
        tasks = _plan_tasks(plan_path)
        print(f"[matrix] dry run: {tasks} task(s) planned in {plan_path}")
        if arguments.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return EXIT_OK
    return _report(summary, output_dir=output_dir, only_json=arguments.json)


def _plan_tasks(plan_path: Path) -> int:
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    tasks = payload.get("tasks")
    return len(tasks) if isinstance(tasks, list) else 0


def _install_stop_request() -> threading.Event:
    """Turn SIGINT/SIGTERM into a cooperative stop instead of an exception.

    Raising ``KeyboardInterrupt`` inside ``asyncio.run`` tears the loop down
    before anything can close the in-flight Run, which is exactly how a stopped
    matrix used to leave that Run ``RUNNING`` with a live container behind it.
    A flag lets the executor reach the official cancel path first.  A second
    signal re-raises, so a process that is not responding can still be killed.
    """

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        print(
            f"\n[matrix] {name}: cancelling the in-flight Run; "
            "send again to abort immediately",
            file=sys.stderr,
        )

    for attribute in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, attribute, None)
        if signum is None:
            continue
        # Windows does not deliver SIGTERM to a console process, and Python
        # raises for a signal it cannot install.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signum, _handle)
    return stop


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    problem = _validate(arguments)
    if problem:
        print(f"[matrix] {problem}", file=sys.stderr)
        return EXIT_CONFIG
    stop = _install_stop_request()
    try:
        return asyncio.run(_run(arguments, stop))
    except ExperimentError as error:
        # A complete artefact without --resume, or a manifest belonging to
        # another configuration: the caller's problem to fix, not a task
        # failure, so it must not be reported as one.
        print(f"[matrix] {error}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("[matrix] interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
