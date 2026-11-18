"""Recoverable case x arm x repeat benchmark experiments.

The module has two deliberately separate layers:

* :class:`ExperimentRunner` owns immutable manifests, raw artifacts, resume,
  atomic writes, and summary generation.
* Executors create and evaluate Runs.  Tests can inject a fake executor
  without importing the database or a model provider.

The real executor writes ``knowledge_mode`` into each ``SolveRun.hints_json``
at creation time.  It never mutates process environment variables.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import platform
import random
import re
import socket
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import comb
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from app.benchmark.case_definition import BenchmarkCase
from app.benchmark.range import available_range, resolve_case_target
from app.schemas.model_config import OPENAI_WIRE_PROVIDER_TYPES, WORKER_PROVIDER_TYPES
from app.training.policy import compute_policy_version

MANIFEST_SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
ARMS = ("off", "on")
MIN_PAIRED_SAMPLES = 10
# One Run's wall-clock budget, and the cumulative one across its attempts.
# A case that never solves otherwise consumes the whole per-Run budget, so the
# wall-clock cost of a matrix is ``runs x max_runtime_seconds`` unless the
# operator pins it lower via ``--runtime-seconds``.
DEFAULT_MAX_RUNTIME_SECONDS = 900
DEFAULT_MAX_TOTAL_RUNTIME_SECONDS = 3600
# The Run owns its own deadline, so the executor normally adds nothing.  This
# guard is the backstop for the case where that deadline does not fire (a
# Coordinator stuck in Finalize, a Worker that never returns): without it a
# single hung Run makes the whole matrix unbounded in wall-clock, which is the
# opposite of what ``--runtime-seconds`` promises.  It is deliberately looser
# than the Run's own budget so the graceful path wins the race in the normal
# case and only a genuinely stuck Run is force-cancelled.
HARD_RUNTIME_GRACE_SECONDS = 180
# How often the guard re-checks a Run that is still executing.  Short enough
# that a Ctrl+C is honoured promptly, long enough to stay free.
RUN_GUARD_POLL_SECONDS = 2.0
# Bootstrap parameters are fixed rather than drawn per call: the acceptance
# criterion for a summary is that it can be recomputed from the raw artifacts,
# and an unseeded resample would give a different interval every time.
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20260101
BOOTSTRAP_ALPHA = 0.05

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_CASE_DIR = Path(__file__).resolve().parent / "cases"
MANIFEST_FILENAME = "manifest.json"
PLAN_FILENAME = "plan.json"
SUMMARY_FILENAME = "summary.json"
ARTIFACT_DIRNAME = "artifacts"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")

logger = logging.getLogger(__name__)


class ExperimentError(RuntimeError):
    """Base error for experiment orchestration failures."""


class ExperimentConfigurationError(ExperimentError):
    """Raised when an existing output directory belongs to another config."""


class ExperimentOutputExistsError(ExperimentError):
    """Raised when an existing complete artifact would be overwritten."""


class ExperimentExecutionError(ExperimentError):
    """Raised by the real executor when a Run cannot be created."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class ExperimentRequest:
    """One case x arm x repeat unit handed to an executor."""

    case: BenchmarkCase
    arm: str
    repeat: int
    output_dir: Path
    case_path: Path | None = None

    @property
    def case_id(self) -> str:
        return self.case.case_id


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Normalized executor result.

    ``evaluation_report=None`` is intentional when execution never reached the
    evaluator.  Callers must not infer success from the Run status alone.
    """

    run_id: str | None
    status: str
    evaluation_report: Mapping[str, Any] | None
    error: str | None = None
    duration_seconds: float | None = None
    tool_calls: int | None = None
    total_tokens: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ExperimentExecutor(Protocol):
    """Executor boundary used by :class:`ExperimentRunner`."""

    async def execute(
        self, request: ExperimentRequest
    ) -> ExecutionResult | Mapping[str, Any]:
        """Execute one experiment request."""


def parse_arms(value: str | Sequence[str]) -> tuple[str, ...]:
    """Parse and validate the fixed off/on arm set."""

    if isinstance(value, str):
        raw_items = [item.strip().lower() for item in value.split(",") if item.strip()]
    else:
        raw_items = [str(item).strip().lower() for item in value if str(item).strip()]
    if not raw_items:
        raise ValueError("At least one arm is required.")
    invalid = sorted(set(raw_items) - set(ARMS))
    if invalid:
        raise ValueError(f"Unsupported arms: {', '.join(invalid)}; expected off/on.")
    repeated = sorted(item for item, count in Counter(raw_items).items() if count > 1)
    if repeated:
        raise ValueError(f"Duplicate arms are not allowed: {', '.join(repeated)}.")
    return tuple(arm for arm in ARMS if arm in raw_items)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def case_definition_hash(case: BenchmarkCase) -> str:
    """Hash the semantic case definition, independent of JSON whitespace."""

    return _sha256_json(case.model_dump(mode="json"))


def _load_case_files(case_dir: Path) -> list[tuple[Path, BenchmarkCase]]:
    if not case_dir.is_dir():
        raise ExperimentConfigurationError(f"Case directory does not exist: {case_dir}")
    loaded: list[tuple[Path, BenchmarkCase]] = []
    seen: set[str] = set()
    for path in sorted(case_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            case = BenchmarkCase.model_validate(payload)
        except Exception as error:
            raise ExperimentConfigurationError(f"Invalid case file {path}: {error}") from error
        if case.case_id in seen:
            raise ExperimentConfigurationError(
                f"Duplicate benchmark case_id in {case_dir}: {case.case_id}"
            )
        seen.add(case.case_id)
        loaded.append((path, case))
    if not loaded:
        raise ExperimentConfigurationError(f"No JSON benchmark cases found in {case_dir}")
    return loaded


def _case_manifest_entries(
    case_files: Sequence[tuple[Path, BenchmarkCase]],
    *,
    case_dir: Path,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path, case in case_files:
        entries.append(
            {
                "case_id": case.case_id,
                "path": str(path.relative_to(case_dir)),
                "sha256": _sha256_bytes(path.read_bytes()),
                "definition_sha256": case_definition_hash(case),
            }
        )
    return entries


def _case_runnable(case: BenchmarkCase, available: str) -> bool:
    """Whether this machine can serve the case's target range."""

    return not case.requires_range or case.requires_range == available


def _git_metadata(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )

    revision = run_git("rev-parse", "HEAD")
    status = run_git("status", "--porcelain")
    return {
        "revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def collect_runtime_metadata(
    executor_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect non-secret runtime information for the immutable manifest."""

    metadata = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "project_root": str(PROJECT_ROOT),
        "git": _git_metadata(),
    }
    if executor_metadata:
        metadata["executor"] = dict(executor_metadata)
    return metadata


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
                default=str,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ExperimentConfigurationError(f"Cannot read JSON file {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ExperimentConfigurationError(f"Expected a JSON object in {path}")
    return payload


def _artifact_name(case_id: str, arm: str, repeat: int) -> str:
    safe_case = _SAFE_NAME.sub("_", case_id).strip("._") or "case"
    return f"{safe_case}__{arm}__r{repeat}.json"


def _artifact_path(output_dir: Path, case_id: str, arm: str, repeat: int) -> Path:
    return output_dir / ARTIFACT_DIRNAME / _artifact_name(case_id, arm, repeat)


class _OutputLock(AbstractContextManager["_OutputLock"]):
    """Small cross-platform single-process lock for one output directory."""

    def __init__(self, output_dir: Path) -> None:
        self.path = output_dir.parent / f".{output_dir.name}.knowledge-experiment.lock"
        self._acquired = False

    def __enter__(self) -> "_OutputLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                break
            except FileExistsError:
                owner = self.path.read_text(encoding="utf-8", errors="replace").strip()
                if not _lock_owner_is_stale(owner):
                    raise ExperimentError(
                        "Output directory is already owned by another runner: "
                        f"{self.path} ({owner})"
                    ) from None
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        if descriptor is None:
            owner = self.path.read_text(encoding="utf-8", errors="replace").strip()
            raise ExperimentError(
                f"Output directory lock could not be acquired: {self.path} ({owner})"
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\nhostname={socket.gethostname()}\n")
        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False


def _lock_owner(owner: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in owner.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip().lower()] = value.strip()
    return fields


def _process_is_running(pid: int) -> bool:
    if pid < 1:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                check=False,
                capture_output=True,
                text=True,
                # ``tasklist`` speaks the console code page, not UTF-8, so under
                # a UTF-8 interpreter the decode fails inside subprocess' reader
                # thread and leaves ``stdout`` as None.  Only ASCII matters here
                # (the PID), so replacing the undecodable bytes is safe.
                errors="replace",
            )
        except OSError:
            return True
        if completed.returncode != 0:
            return True
        for line in (completed.stdout or "").splitlines():
            columns = [column.strip().strip('"') for column in line.split(",")]
            if len(columns) > 1 and columns[1] == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _lock_owner_is_stale(owner: str) -> bool:
    """Return true only when a local lock definitely has no owning process."""

    fields = _lock_owner(owner)
    try:
        pid = int(fields.get("pid", ""))
    except ValueError:
        return True
    hostname = fields.get("hostname", "")
    if hostname and hostname.casefold() != socket.gethostname().casefold():
        return False
    return not _process_is_running(pid)


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested_mapping(value: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _report_duration(report: Mapping[str, Any] | None) -> float | None:
    if not report:
        return None
    return _numeric(_nested_mapping(report, "metrics", "efficiency").get("duration_seconds"))


def _report_tool_calls(report: Mapping[str, Any] | None) -> int | None:
    if not report:
        return None
    efficiency = _nested_mapping(report, "metrics", "efficiency")
    agent = _nested_mapping(report, "metrics", "agent")
    value = efficiency.get("tool_calls")
    if value is None:
        value = agent.get("tool_calls")
    number = _numeric(value)
    return int(number) if number is not None else None


def _report_total_tokens(report: Mapping[str, Any] | None) -> int | None:
    if not report:
        return None
    efficiency = _nested_mapping(report, "metrics", "efficiency")
    value = efficiency.get("total_tokens")
    if value is None:
        input_tokens = _numeric(efficiency.get("input_tokens"))
        output_tokens = _numeric(efficiency.get("output_tokens"))
        if input_tokens is None and output_tokens is None:
            return None
        value = (input_tokens or 0.0) + (output_tokens or 0.0)
    number = _numeric(value)
    return int(number) if number is not None else None


def _report_turns_used(report: Mapping[str, Any] | None) -> int | None:
    """Agent turns consumed, as the evaluator counted them."""
    if not report:
        return None
    number = _numeric(report.get("turns_used"))
    return int(number) if number is not None else None


def _report_solved_within_budget(report: Mapping[str, Any] | None) -> bool | None:
    """Tri-state: True / False / None when the case declared no budget."""
    if not report:
        return None
    value = report.get("solved_within_budget")
    return value if isinstance(value, bool) else None


def _exact_mcnemar_p(arm_on_only: int, arm_off_only: int) -> float | None:
    """Two-sided exact McNemar p-value over the discordant pairs.

    McNemar reduces to a sign test on the pairs the two arms disagree about, so
    the exact form is a two-sided binomial test at p=0.5 — and ``math.comb``
    ships with the standard library, which is the whole implementation.  The
    exact form is chosen over an asymptotic approximation because the sample
    sizes this experiment can realistically afford are small, which is exactly
    where the approximation is worst.
    """

    total = arm_on_only + arm_off_only
    if total <= 0:
        return None
    tail = sum(comb(total, k) for k in range(min(arm_on_only, arm_off_only) + 1)) / (
        2**total
    )
    return round(min(1.0, 2 * tail), 6)


def _bootstrap_mean_ci(
    values: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = BOOTSTRAP_ALPHA,
) -> list[float] | None:
    """Percentile bootstrap CI for the mean of a paired-difference sample."""

    if not values:
        return None
    rng = random.Random(seed)
    size = len(values)
    means = sorted(
        sum(rng.choice(values) for _ in range(size)) / size for _ in range(iterations)
    )
    return [
        round(means[max(int((alpha / 2) * iterations), 0)], 6),
        round(means[min(int((1 - alpha / 2) * iterations), iterations) - 1], 6),
    ]


def is_successful(record: Mapping[str, Any]) -> bool:
    """Return evaluator-backed success, never Run-status success."""

    report = record.get("evaluation_report")
    if not isinstance(report, Mapping):
        return False
    return str(report.get("final_result") or "").upper() == "SUCCESS"


def _normalize_result(value: Any) -> ExecutionResult:
    if isinstance(value, ExecutionResult):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("Executor must return ExecutionResult or a mapping.")
    report = value.get("evaluation_report")
    if report is None:
        report = value.get("evaluation")
    if report is None:
        report = value.get("report")
    if report is not None and not isinstance(report, Mapping):
        raise TypeError("Executor evaluation_report must be a mapping or None.")
    status = str(value.get("status") or ("EXECUTION_ERROR" if value.get("error") else "UNKNOWN"))
    run_id = value.get("run_id", value.get("id"))
    metadata = value.get("metadata")
    return ExecutionResult(
        run_id=str(run_id) if run_id is not None else None,
        status=status,
        evaluation_report=dict(report) if report is not None else None,
        error=str(value["error"]) if value.get("error") else None,
        duration_seconds=_numeric(value.get("duration_seconds")),
        tool_calls=(
            int(_numeric(value["tool_calls"]))
            if _numeric(value.get("tool_calls")) is not None
            else None
        ),
        total_tokens=(
            int(_numeric(value["total_tokens"]))
            if _numeric(value.get("total_tokens")) is not None
            else None
        ),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _call_supported(
    function: Callable[..., Any],
    request: ExperimentRequest,
) -> Any:
    candidates = (
        (request,),
        (request.case, request.arm, request.repeat),
        (request.case, request.arm, request.repeat, request.output_dir),
    )
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        for candidate in candidates:
            try:
                signature.bind(*candidate)
            except TypeError:
                continue
            return function(*candidate)
        raise TypeError(
            "Executor execute method must accept ExperimentRequest or "
            "(case, arm, repeat[, output_dir])."
        )
    return function(request)


async def _invoke_executor(
    executor: ExperimentExecutor,
    request: ExperimentRequest,
) -> ExecutionResult:
    function = getattr(executor, "execute", None)
    if function is None and callable(executor):
        function = executor
    if function is None:
        raise TypeError("Executor must define execute() or be callable.")
    value = _call_supported(function, request)
    if inspect.isawaitable(value):
        value = await value
    return _normalize_result(value)


async def _executor_preflight(
    executor: ExperimentExecutor,
) -> Mapping[str, Any]:
    function = getattr(executor, "preflight", None)
    if function is None:
        return {}
    try:
        inspect.signature(function).bind()
    except (TypeError, ValueError):
        raise TypeError("Executor preflight method must accept no arguments.") from None
    value = function()
    if inspect.isawaitable(value):
        value = await value
    return dict(value) if isinstance(value, Mapping) else {}


def _record_metrics(
    result: ExecutionResult,
) -> tuple[float | None, int | None, int | None]:
    duration = result.duration_seconds
    if duration is None:
        duration = _report_duration(result.evaluation_report)
    tool_calls = result.tool_calls
    if tool_calls is None:
        tool_calls = _report_tool_calls(result.evaluation_report)
    total_tokens = result.total_tokens
    if total_tokens is None:
        total_tokens = _report_total_tokens(result.evaluation_report)
    return duration, tool_calls, total_tokens


def _raw_record(
    request: ExperimentRequest,
    *,
    configuration_fingerprint: str,
    result: ExecutionResult,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    duration, tool_calls, total_tokens = _record_metrics(result)
    report = dict(result.evaluation_report) if result.evaluation_report is not None else None
    record: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "complete": True,
        "configuration_fingerprint": configuration_fingerprint,
        "case_id": request.case_id,
        "arm": request.arm,
        "repeat": request.repeat,
        "run_id": result.run_id,
        "status": result.status,
        "evaluation_report": report,
        "error": result.error,
        "success": bool(report and is_successful({"evaluation_report": report})),
        "duration_seconds": duration,
        "tool_calls": tool_calls,
        "total_tokens": total_tokens,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
    }
    if result.metadata:
        record["metadata"] = dict(result.metadata)
    return record


def _validate_artifact(
    payload: Mapping[str, Any],
    *,
    request: ExperimentRequest,
    configuration_fingerprint: str,
) -> bool:
    required = {
        "schema_version",
        "complete",
        "configuration_fingerprint",
        "case_id",
        "arm",
        "repeat",
        "run_id",
        "status",
        "evaluation_report",
        "error",
    }
    if not required.issubset(payload):
        return False
    if payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        return False
    if payload.get("complete") is not True:
        return False
    if payload.get("configuration_fingerprint") != configuration_fingerprint:
        raise ExperimentConfigurationError(
            "Existing raw artifact belongs to another configuration: "
            f"{request.case_id}/{request.arm}/repeat-{request.repeat}"
        )
    return (
        payload.get("case_id") == request.case_id
        and payload.get("arm") == request.arm
        and payload.get("repeat") == request.repeat
    )


def _load_artifact(
    path: Path,
    *,
    request: ExperimentRequest,
    configuration_fingerprint: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = _read_json(path)
    except ExperimentConfigurationError:
        return None
    if not _validate_artifact(
        payload,
        request=request,
        configuration_fingerprint=configuration_fingerprint,
    ):
        return None
    return payload


def _average(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _process_metric_summary(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-Run process metrics across one arm.

    Both metrics are negative indicators (see ``app/services/run_process_metrics``);
    they are reported as ``None`` rather than ``0`` when nothing was measurable,
    so "the off arm never received a prior" cannot be read as "the off arm was
    never misled".
    """

    records = [
        metrics
        for record in selected
        if isinstance(metadata := record.get("metadata"), Mapping)
        and isinstance(metrics := metadata.get("process_metrics"), Mapping)
    ]
    repeat_ratios: list[float] = []
    same_shape_ratios: list[float] = []
    total_calls = 0
    repeat_calls = 0
    same_shape_calls = 0
    prior_runs = 0
    graph_runs = 0
    carrying = 0
    misled = 0
    for metrics in records:
        repeat = metrics.get("repeat_attempts")
        if isinstance(repeat, Mapping):
            ratio = _numeric(repeat.get("repeat_ratio"))
            if ratio is not None:
                repeat_ratios.append(ratio)
            shape_ratio = _numeric(repeat.get("same_shape_ratio"))
            if shape_ratio is not None:
                same_shape_ratios.append(shape_ratio)
            total_calls += int(_numeric(repeat.get("total_calls")) or 0)
            repeat_calls += int(_numeric(repeat.get("repeat_calls")) or 0)
            same_shape_calls += int(_numeric(repeat.get("same_shape_calls")) or 0)
        prior = metrics.get("prior_misled")
        if isinstance(prior, Mapping):
            if int(_numeric(prior.get("graph_events")) or 0) > 0:
                graph_runs += 1
            run_carrying = int(_numeric(prior.get("prior_carrying_intents")) or 0)
            if run_carrying:
                prior_runs += 1
                carrying += run_carrying
                misled += int(_numeric(prior.get("misled_intents")) or 0)
    return {
        "runs_measured": len(records),
        "repeat_attempts": {
            "mean_repeat_ratio": _average(repeat_ratios),
            "pooled_repeat_ratio": (
                round(repeat_calls / total_calls, 6) if total_calls else None
            ),
            "total_calls": total_calls,
            "repeat_calls": repeat_calls,
            # Weaker signal, reported next to the exact one: a tool used with 20
            # different URLs shares one shape, so read it with the number above.
            "mean_same_shape_ratio": _average(same_shape_ratios),
            "pooled_same_shape_ratio": (
                round(same_shape_calls / total_calls, 6) if total_calls else None
            ),
            "same_shape_calls": same_shape_calls,
        },
        "prior_misled": {
            "runs_with_graph": graph_runs,
            # 0 here means "no prior ever reached a Worker" (the off arm), not
            # "no harm was done" — which is why the rate below stays None.
            "runs_that_received_a_prior": prior_runs,
            "prior_carrying_intents": carrying,
            "misled_intents": misled,
            "pooled_misled_rate": (
                round(misled / carrying, 6) if carrying else None
            ),
        },
    }


def _arm_statistics(
    records: Sequence[Mapping[str, Any]],
    *,
    arm: str,
) -> dict[str, Any]:
    selected = [record for record in records if record.get("arm") == arm]
    successes = sum(bool(record.get("success")) for record in selected)
    evaluations = sum(
        isinstance(record.get("evaluation_report"), Mapping) for record in selected
    )
    durations = [
        number
        for record in selected
        if (number := _numeric(record.get("duration_seconds"))) is not None
    ]
    tool_calls = [
        number
        for record in selected
        if (number := _numeric(record.get("tool_calls"))) is not None
    ]
    tokens = [
        number
        for record in selected
        if (number := _numeric(record.get("total_tokens"))) is not None
    ]
    reports = [
        report
        for record in selected
        if isinstance(report := record.get("evaluation_report"), Mapping)
    ]
    turns_used = [
        number
        for report in reports
        if (number := _report_turns_used(report)) is not None
    ]
    within_budget = [
        value
        for report in reports
        if (value := _report_solved_within_budget(report)) is not None
    ]
    return {
        "arm": arm,
        "attempts": len(selected),
        "success": successes,
        "success_rate": round(successes / len(selected), 6) if selected else None,
        "evaluated_attempts": evaluations,
        # Reported over evaluated attempts only: an attempt the evaluator could
        # not score is not a failure, and folding it in would understate the arm.
        "pass_at_1": round(successes / evaluations, 6) if evaluations else None,
        "average_duration_seconds": _average(durations),
        "average_tool_calls": _average(tool_calls),
        "average_tokens": _average(tokens),
        "average_turns_used": _average([float(number) for number in turns_used]),
        "turns_used_distribution": dict(sorted(Counter(turns_used).items())),
        "solved_within_budget": {
            "true": sum(1 for value in within_budget if value),
            "false": sum(1 for value in within_budget if not value),
            # Cases that declare no budget leave this genuinely unknown.
            "unknown": len(reports) - len(within_budget),
        },
        "metric_sample_counts": {
            "duration_seconds": len(durations),
            "tool_calls": len(tool_calls),
            "total_tokens": len(tokens),
            "turns_used": len(turns_used),
        },
        "final_status_distribution": dict(
            sorted(Counter(str(record.get("status") or "UNKNOWN") for record in selected).items())
        ),
        "errors": sum(bool(record.get("error")) for record in selected),
        "process_metrics": _process_metric_summary(selected),
    }


def _paired_effect(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    indexed: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for record in records:
        try:
            repeat = int(record["repeat"])
        except (KeyError, TypeError, ValueError):
            continue
        case_id = str(record.get("case_id") or "")
        arm = str(record.get("arm") or "")
        if case_id and arm in ARMS:
            indexed[(case_id, repeat)][arm] = record

    deltas: list[float] = []
    by_case: dict[str, list[float]] = defaultdict(list)
    for (case_id, _repeat), pair in sorted(indexed.items()):
        if set(pair) != set(ARMS):
            continue
        delta = float(bool(pair["on"].get("success"))) - float(
            bool(pair["off"].get("success"))
        )
        deltas.append(delta)
        by_case[case_id].append(delta)

    sample_count = len(deltas)
    adequate = sample_count >= MIN_PAIRED_SAMPLES
    warning = None
    if not adequate:
        warning = (
            f"Only {sample_count} paired case/repeat samples are available; "
            f"at least {MIN_PAIRED_SAMPLES} are required before making a "
            "performance claim. The reported delta is descriptive only."
        )
    # The sign split is the McNemar contingency table: pairs the two arms
    # disagree about are the only ones that carry information, and the exact
    # test is withheld below the sample floor because an exact p-value on three
    # discordant pairs is computable and meaningless.
    on_only = sum(1 for delta in deltas if delta > 0)
    off_only = sum(1 for delta in deltas if delta < 0)
    p_value = _exact_mcnemar_p(on_only, off_only) if adequate else None
    return {
        "definition": "mean(on success - off success) over matching case_id/repeat pairs",
        "success_delta": _average(deltas),
        "on_minus_off_success_delta": _average(deltas),
        "success_delta_bootstrap_ci_95": _bootstrap_mean_ci(deltas) if adequate else None,
        "bootstrap": {
            "iterations": BOOTSTRAP_ITERATIONS,
            "seed": BOOTSTRAP_SEED,
            "alpha": BOOTSTRAP_ALPHA,
        },
        "mcnemar": {
            "test": "exact_mcnemar_two_sided",
            "on_only_success": on_only,
            "off_only_success": off_only,
            "concordant_pairs": sample_count - on_only - off_only,
            "p_value": p_value,
            "reported": adequate,
            "significant": bool(p_value is not None and p_value < BOOTSTRAP_ALPHA),
        },
        "sample_count": sample_count,
        "pairs": sample_count,
        "minimum_recommended_samples": MIN_PAIRED_SAMPLES,
        "sample_adequate": adequate,
        "claim": "descriptive_only" if not adequate else "descriptive",
        "warning": warning,
        "by_case": {
            case_id: {
                "sample_count": len(case_deltas),
                "mean_success_delta": _average(case_deltas),
            }
            for case_id, case_deltas in sorted(by_case.items())
        },
    }


def build_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    arms: Sequence[str] = ARMS,
    configuration_fingerprint: str | None = None,
    skipped_cases: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Rebuild a summary from raw artifact records."""

    normalized_arms = tuple(arm for arm in ARMS if arm in set(arms))
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration_fingerprint": configuration_fingerprint,
        "success_definition": (
            "evaluation_report.final_result == 'SUCCESS'; Run status alone is not success"
        ),
        "record_count": len(records),
        # Kept next to the rates on purpose: a suite that skipped half its cases
        # must not read as a suite that solved all of them.
        "skipped_case_count": len(skipped_cases),
        "skipped_cases": [dict(entry) for entry in skipped_cases],
        "by_arm": {
            arm: _arm_statistics(records, arm=arm) for arm in normalized_arms
        },
        "paired_effect": _paired_effect(records),
        "limitations": [
            (
                "Paired effects are exposed with an exact McNemar p-value and a "
                "bootstrap interval only once the paired sample clears "
                f"{MIN_PAIRED_SAMPLES}; below that the delta is descriptive only."
            ),
            "Missing metrics are excluded from the corresponding average and counted separately.",
            (
                "The interval covers sampling noise across repetitions, not "
                "variety across cases: these cases share one range, so it says "
                "nothing about generalisation to other targets."
            ),
            (
                "Process metrics describe how a Run searched, not whether it won; "
                "they are reported as null when nothing was measurable, so a null "
                "must never be read as a good score."
            ),
        ],
    }


def load_raw_artifacts(output_dir: str | Path) -> list[dict[str, Any]]:
    """Read valid JSON artifacts from an experiment directory."""

    directory = Path(output_dir) / ARTIFACT_DIRNAME
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = _read_json(path)
        except ExperimentConfigurationError:
            continue
        if payload.get("schema_version") == ARTIFACT_SCHEMA_VERSION:
            records.append(payload)
    return records


def configuration_fingerprint(
    *,
    cases: Sequence[Mapping[str, Any]],
    arms: Sequence[str],
    repeats: int,
    executor_name: str,
    runtime_selection: Mapping[str, Any] | None = None,
) -> str:
    """Hash all resume-relevant experiment configuration."""

    return _sha256_json(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "cases": list(cases),
            "arms": list(arms),
            "repeats": repeats,
            "executor": executor_name,
            "runtime_selection": dict(runtime_selection or {}),
        }
    )


@dataclass(slots=True)
class ExperimentConfig:
    """Runner configuration."""

    case_dir: Path
    output_dir: Path
    arms: tuple[str, ...] = ARMS
    repeats: int = 1
    resume: bool = False
    dry_run: bool = False
    executor_name: str = "injected"
    runtime_selection: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.case_dir = Path(self.case_dir).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.arms = parse_arms(self.arms)
        if self.repeats < 1:
            raise ValueError("repeats must be at least 1")
        self.runtime_selection = dict(self.runtime_selection)


class ExperimentRunner:
    """Run or resume a deterministic case x arm x repeat experiment."""

    def __init__(
        self,
        *,
        case_dir: str | Path,
        output_dir: str | Path,
        executor: ExperimentExecutor,
        arms: str | Sequence[str] = ARMS,
        repeats: int = 1,
        resume: bool = False,
        dry_run: bool = False,
        executor_name: str | None = None,
        runtime_selection: Mapping[str, Any] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        name = executor_name or type(executor).__name__
        self.config = ExperimentConfig(
            case_dir=Path(case_dir),
            output_dir=Path(output_dir),
            arms=parse_arms(arms),
            repeats=repeats,
            resume=resume,
            dry_run=dry_run,
            executor_name=name,
            runtime_selection=runtime_selection or {},
        )
        self.executor = executor
        # Checked between tasks, never inside one: the in-flight Run is the
        # executor's responsibility (it holds the supervisor handle), while an
        # operator stopping a matrix must not start the *next* task.
        self._stop_requested = stop_requested or (lambda: False)

    async def run(self) -> dict[str, Any]:
        case_files = _load_case_files(self.config.case_dir)
        case_entries = _case_manifest_entries(
            case_files,
            case_dir=self.config.case_dir,
        )
        fingerprint = configuration_fingerprint(
            cases=case_entries,
            arms=self.config.arms,
            repeats=self.config.repeats,
            executor_name=self.config.executor_name,
            runtime_selection=self.config.runtime_selection,
        )

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        with _OutputLock(self.config.output_dir):
            manifest = await self._ensure_manifest(
                fingerprint=fingerprint,
                case_entries=case_entries,
            )
            requests = self._requests(case_files)
            skipped = self._skipped_cases(case_files)
            self._write_plan(
                fingerprint=fingerprint,
                requests=requests,
                manifest=manifest,
                skipped=skipped,
            )

            if self.config.dry_run:
                return build_summary(
                    load_raw_artifacts(self.config.output_dir),
                    arms=self.config.arms,
                    configuration_fingerprint=fingerprint,
                    skipped_cases=skipped,
                )

            records: list[dict[str, Any]] = []
            stopped_early = False
            for request in requests:
                if self._stop_requested():
                    # Stop between tasks, not between a task and its artifact:
                    # the task that was interrupted already wrote its own
                    # record, and an unwritten request is simply not attempted.
                    stopped_early = True
                    break
                path = _artifact_path(
                    self.config.output_dir,
                    request.case_id,
                    request.arm,
                    request.repeat,
                )
                existing = _load_artifact(
                    path,
                    request=request,
                    configuration_fingerprint=fingerprint,
                )
                if existing is not None:
                    if self.config.resume:
                        records.append(existing)
                        continue
                    raise ExperimentOutputExistsError(
                        f"Complete artifact already exists; use --resume: {path}"
                    )

                started_at = datetime.now(UTC)
                try:
                    result = await _invoke_executor(self.executor, request)
                except ExperimentExecutionError as error:
                    result = ExecutionResult(
                        run_id=error.run_id,
                        status="EXECUTION_ERROR",
                        evaluation_report=None,
                        error=str(error),
                    )
                except Exception as error:
                    result = ExecutionResult(
                        run_id=None,
                        status="EXECUTION_ERROR",
                        evaluation_report=None,
                        error=f"{type(error).__name__}: {error}",
                    )
                finished_at = datetime.now(UTC)
                record = _raw_record(
                    request,
                    configuration_fingerprint=fingerprint,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                _atomic_write_json(path, record)
                records.append(record)

            summary = build_summary(
                records,
                arms=self.config.arms,
                configuration_fingerprint=fingerprint,
                skipped_cases=skipped,
            )
            if stopped_early:
                # Never leave this implicit.  A partial matrix that looks like a
                # complete one is worse than no matrix: the pass rate of the
                # remaining tasks would be read as the pass rate of the whole
                # plan.
                summary = {
                    **summary,
                    "stopped_early": True,
                    "completed_tasks": len(records),
                    "planned_tasks": len(requests),
                }
            _atomic_write_json(self.config.output_dir / SUMMARY_FILENAME, summary)
            return summary

    def _skipped_cases(
        self,
        case_files: Sequence[tuple[Path, BenchmarkCase]],
    ) -> list[dict[str, Any]]:
        """Cases this machine cannot serve, recorded as skipped rather than failed.

        Without this a case whose target range does not exist would be attempted,
        fail on connection, and be averaged into the solve rate as though the
        solver had failed it.
        """

        available = available_range()
        return [
            {
                "case_id": case.case_id,
                "requires_range": case.requires_range,
                "available_range": available,
                "status": "SKIPPED_REQUIRES_RANGE_MISSING",
            }
            for _path, case in case_files
            if not _case_runnable(case, available)
        ]

    def _requests(
        self,
        case_files: Sequence[tuple[Path, BenchmarkCase]],
    ) -> list[ExperimentRequest]:
        available = available_range()
        requests: list[ExperimentRequest] = []
        for repeat in range(1, self.config.repeats + 1):
            for path, case in case_files:
                if not _case_runnable(case, available):
                    continue
                for arm in self.config.arms:
                    requests.append(
                        ExperimentRequest(
                            case=case,
                            arm=arm,
                            repeat=repeat,
                            output_dir=self.config.output_dir,
                            case_path=path,
                        )
                    )
        return requests

    async def _ensure_manifest(
        self,
        *,
        fingerprint: str,
        case_entries: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        manifest_path = self.config.output_dir / MANIFEST_FILENAME
        if manifest_path.is_file():
            manifest = _read_json(manifest_path)
            recorded = manifest.get("configuration_fingerprint")
            if recorded != fingerprint:
                raise ExperimentConfigurationError(
                    "Existing manifest configuration does not match the requested "
                    f"experiment: {manifest_path}"
                )
            if manifest.get("immutable") is not True:
                raise ExperimentConfigurationError(
                    f"Existing manifest is not marked immutable: {manifest_path}"
                )
            current_preflight = await _executor_preflight(self.executor)
            recorded_preflight = _nested_mapping(
                manifest,
                "runtime",
                "executor",
                "preflight",
            )
            if current_preflight and recorded_preflight != current_preflight:
                raise ExperimentConfigurationError(
                    "Resolved executor preflight configuration no longer matches "
                    f"the immutable manifest: {manifest_path}"
                )
            recorded_policy = (manifest.get("policy") or {}).get("policy_version")
            current_policy = compute_policy_version()["policy_version"]
            if recorded_policy and recorded_policy != current_policy:
                # Not fatal: a resumed experiment must be able to finish on the
                # code it started with, and the manifest already pins the id
                # that was live at creation time.  The warning is what keeps a
                # stray edit from silently mixing two policies into one result.
                logger.warning(
                    "policy version changed since manifest creation: %s -> %s (%s)",
                    recorded_policy,
                    current_policy,
                    manifest_path,
                )
            return manifest

        preflight = await _executor_preflight(self.executor)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "immutable": True,
            "created_at": datetime.now(UTC).isoformat(),
            "configuration_fingerprint": fingerprint,
            "cases": list(case_entries),
            "arms": list(self.config.arms),
            "repeats": self.config.repeats,
            "runtime_selection": dict(self.config.runtime_selection),
            # Pins the solver strategy itself (planner, Worker discipline,
            # retrieval scoring).  Without this, two manifests with the same
            # cases and arms can describe two different solvers.
            "policy": compute_policy_version(extra={"arms": ",".join(self.config.arms)}),
            "runtime": collect_runtime_metadata(
                {
                    "name": self.config.executor_name,
                    **({
                        "preflight": preflight
                        if preflight
                        else {"status": "not_available"}
                    }),
                }
            ),
        }
        _atomic_write_json(manifest_path, manifest)
        return manifest

    def _write_plan(
        self,
        *,
        fingerprint: str,
        requests: Sequence[ExperimentRequest],
        manifest: Mapping[str, Any],
        skipped: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        plan = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "configuration_fingerprint": fingerprint,
            "manifest_created_at": manifest.get("created_at"),
            "dry_run": self.config.dry_run,
            "resume": self.config.resume,
            "skipped_cases": [dict(entry) for entry in skipped],
            "tasks": [
                {
                    "case_id": request.case_id,
                    "arm": request.arm,
                    "repeat": request.repeat,
                    "artifact": str(
                        _artifact_path(
                            self.config.output_dir,
                            request.case_id,
                            request.arm,
                            request.repeat,
                        ).relative_to(self.config.output_dir)
                    ),
                }
                for request in requests
            ],
        }
        _atomic_write_json(self.config.output_dir / PLAN_FILENAME, plan)


class RealRunExecutor:
    """Create a durable SolveRun and execute it through run_supervisor."""

    def __init__(
        self,
        *,
        model_config_id: str | None = None,
        reason_model_config_id: str | None = None,
        solver_mode: str = "muteki",
        max_agent_steps: int = 120,
        max_tool_calls: int = 120,
        max_context_observations: int = 8,
        max_runtime_seconds: int | None = None,
        max_total_runtime_seconds: int | None = None,
        hard_runtime_seconds: int | None = None,
        stop_requested: Callable[[], bool] | None = None,
        session_factory: Any | None = None,
        supervisor: Any | None = None,
    ) -> None:
        self.model_config_id = model_config_id
        self.reason_model_config_id = reason_model_config_id
        self.solver_mode = solver_mode
        self.max_agent_steps = max_agent_steps
        self.max_tool_calls = max_tool_calls
        self.max_context_observations = max_context_observations
        # ``None`` means "use the default", never "zero": the CLI forwards the
        # flag straight through, and a flag that was not supplied must not
        # collapse a Run's budget to nothing.
        self.max_runtime_seconds = int(
            max_runtime_seconds if max_runtime_seconds is not None else DEFAULT_MAX_RUNTIME_SECONDS
        )
        self.max_total_runtime_seconds = int(
            max_total_runtime_seconds
            if max_total_runtime_seconds is not None
            else DEFAULT_MAX_TOTAL_RUNTIME_SECONDS
        )
        # Executor-side wall clock.  ``None`` derives it from the Run's own
        # budget instead of disabling it: an unbounded executor is precisely the
        # failure mode this exists to remove.
        self.hard_runtime_seconds = int(
            hard_runtime_seconds
            if hard_runtime_seconds is not None
            else self.max_runtime_seconds + HARD_RUNTIME_GRACE_SECONDS
        )
        # A callback rather than an ``asyncio.Event``: the caller may be a CLI
        # signalled by the operating system, and a ``threading.Event`` is the
        # only kind that can be set from a signal handler safely.
        self._stop_requested = stop_requested or (lambda: False)
        self._session_factory = session_factory
        self._supervisor = supervisor
        self._preflight: dict[str, Any] | None = None

    def manifest_metadata(self) -> dict[str, Any]:
        return {
            "name": "real_run_supervisor",
            "solver_mode": self.solver_mode,
            "model_config_id": self.model_config_id,
            "reason_model_config_id": self.reason_model_config_id,
            "limits": {
                "max_agent_steps": self.max_agent_steps,
                "max_tool_calls": self.max_tool_calls,
                "max_context_observations": self.max_context_observations,
                "max_runtime_seconds": self.max_runtime_seconds,
                "max_total_runtime_seconds": self.max_total_runtime_seconds,
            },
        }

    def runtime_selection(self) -> dict[str, Any]:
        return {
            "solver_mode": self.solver_mode,
            "model_config_id": self.model_config_id,
            "reason_model_config_id": self.reason_model_config_id,
            "limits": self.manifest_metadata()["limits"],
        }

    async def preflight(self) -> Mapping[str, Any]:
        if self._preflight is not None:
            return dict(self._preflight)
        try:
            session_factory = self._get_session_factory()
            async with session_factory() as session:
                worker, reason = await self._resolve_models(session)
            self._preflight = {
                "status": "ready",
                "worker_model": _model_snapshot(worker),
                "reason_model": _model_snapshot(reason) if reason is not None else None,
            }
        except Exception as error:
            self._preflight = {
                "status": "error",
                "error": f"{type(error).__name__}: {error}",
                "model_config_id": self.model_config_id,
                "reason_model_config_id": self.reason_model_config_id,
            }
        return dict(self._preflight)

    async def _drive_run(self, supervisor: Any, run_id: str) -> Any:
        """Drive one Run under a wall-clock guard and an operator stop request.

        Returns the supervisor's ``RunOutcome`` when the Run reached its own
        terminal state, so the caller keeps the ordinary evaluation path.
        Returns an ``ExecutionResult`` when the guard fired; the Run has then
        been force-cancelled and closed, and must not be scored.

        ``supervisor.run_background`` is awaited here rather than directly
        because it has no upper bound of its own: a Run whose durable deadline
        never fires keeps this process alive indefinitely, and an interrupted
        CLI leaves that Run in ``RUNNING`` along with its container.  Polling
        costs one wake-up per ``RUN_GUARD_POLL_SECONDS`` and buys both
        behaviours.
        """

        task = asyncio.create_task(supervisor.run_background(run_id))
        deadline = time.monotonic() + self.hard_runtime_seconds
        while True:
            done, _ = await asyncio.wait({task}, timeout=RUN_GUARD_POLL_SECONDS)
            if done:
                return await task
            reason = None
            if self._stop_requested():
                reason = "BENCHMARK_INTERRUPTED"
            elif time.monotonic() >= deadline:
                reason = "RUN_HARD_TIMEOUT"
            if reason is not None:
                return await self._abandon(supervisor, run_id, task, reason=reason)

    async def _abandon(
        self,
        supervisor: Any,
        run_id: str,
        task: asyncio.Task[Any],
        *,
        reason: str,
    ) -> ExecutionResult:
        """Force-cancel one Run and make its durable row agree.

        ``cancel_muteki_with_handoff`` stops the native runtime and tears the
        run container down, but it deliberately does not write the terminal Run
        row -- the API cancel endpoint does that afterwards.  A CLI has no
        endpoint, so it must do the second half itself: without it the Run stays
        ``RUNNING`` forever and the next status query reports a phantom solve in
        flight.
        """

        logger.warning("Aborting benchmark Run run_id=%s reason=%s", run_id, reason)
        try:
            await supervisor.cancel_muteki_with_handoff(run_id)
        except Exception:
            # The teardown is idempotent and the terminal write below still
            # applies.  Failing to report the abort because the cancel path
            # itself complained would hide the Run behind a generic executor
            # error.
            logger.exception("Muteki cancel handoff failed run_id=%s", run_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        from app.orchestration.state_machine import TERMINAL, parse_run_status

        session_factory = self._get_session_factory()
        async with session_factory() as session:
            persisted = await session.get(_solve_run_class(), run_id)
            current = str(getattr(persisted, "status", "") or "")
            if parse_run_status(current) in TERMINAL:
                # The Run closed itself while the guard was firing.  Rewriting
                # it as CANCELLED would throw away a real result.
                return ExecutionResult(
                    run_id=run_id,
                    status=current,
                    evaluation_report=None,
                    error=reason,
                )
            if persisted is None:
                return ExecutionResult(
                    run_id=run_id,
                    status="CANCELLED",
                    evaluation_report=None,
                    error=reason,
                )
            from app.services.run_lifecycle import cancel_run

            await cancel_run(session, run_id, f"Benchmark aborted: {reason}")
        return ExecutionResult(
            run_id=run_id,
            status="CANCELLED",
            evaluation_report=None,
            error=reason,
        )

    async def execute(self, request: ExperimentRequest) -> ExecutionResult:
        preflight = await self.preflight()
        if preflight.get("status") != "ready":
            raise ExperimentExecutionError(
                "MODEL_CONFIG_REQUIRED",
                str(preflight.get("error") or "Real execution preflight failed."),
            )

        session_factory = self._get_session_factory()
        supervisor = self._get_supervisor()
        run_id: str | None = None
        try:
            async with session_factory() as session:
                worker, reason = await self._resolve_models(session)
                challenge = await self._get_or_create_challenge(
                    session,
                    request=request,
                )
                run = await self._create_run(
                    session,
                    challenge=challenge,
                    worker=worker,
                    reason=reason,
                    request=request,
                )
                run_id = str(run.id)
                await session.commit()

            outcome = await self._drive_run(supervisor, run_id)
            if isinstance(outcome, ExecutionResult):
                # The guard fired: the Run was force-cancelled and closed.  It
                # is not a solve attempt, so the evaluator must not be asked to
                # score it -- ``evaluation_report=None`` is what tells the
                # summary "this was aborted", which is different from "solved
                # nothing".
                return outcome
            status = str(getattr(outcome, "status", "UNKNOWN"))
            error_code = getattr(outcome, "error_code", None)

            async with session_factory() as session:
                persisted = await session.get(_solve_run_class(), run_id)
                error_message = (
                    str(getattr(persisted, "last_error_message", "") or "")
                    if persisted is not None
                    else ""
                )
                try:
                    from app.benchmark.evaluation import evaluate_session

                    report = await evaluate_session(
                        session,
                        run_id,
                        request.case,
                    )
                except Exception as error:
                    report = None
                    error_message = f"EVALUATION_FAILED: {type(error).__name__}: {error}"
                if report is not None and persisted is not None:
                    await _persist_evaluation_report(
                        session,
                        persisted,
                        report,
                    )
                process_metrics: dict[str, Any] = {}
                try:
                    from app.services.run_process_metrics import (
                        collect_run_process_metrics,
                    )

                    process_metrics = await collect_run_process_metrics(
                        session,
                        run_id,
                        workspace_path=getattr(persisted, "workspace_path", None),
                    )
                except Exception as error:
                    # Visible, never silent: the failure travels inside the
                    # record so "no metric was computed" stays distinguishable
                    # from "the metric was zero".
                    process_metrics = {
                        "error": f"PROCESS_METRICS_FAILED: {type(error).__name__}: {error}"
                    }

            error = None
            if status not in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED"}:
                error = _execution_error_text(
                    error_code=error_code,
                    error_message=error_message,
                    status=status,
                )
            return ExecutionResult(
                run_id=run_id,
                status=status,
                evaluation_report=report,
                error=error,
                metadata={
                    "challenge_id": str(challenge.id),
                    "process_metrics": process_metrics,
                },
            )
        except ExperimentExecutionError:
            raise
        except Exception as error:
            raise ExperimentExecutionError(
                "REAL_RUN_FAILED",
                f"{type(error).__name__}: {error}",
                run_id=run_id,
            ) from error

    def _get_session_factory(self) -> Callable[[], Any]:
        if self._session_factory is not None:
            return self._session_factory
        from app.core.database import SessionLocal

        self._session_factory = SessionLocal
        return SessionLocal

    def _get_supervisor(self) -> Any:
        if self._supervisor is not None:
            return self._supervisor
        from app.services.run_supervisor import run_supervisor

        self._supervisor = run_supervisor
        return run_supervisor

    async def _resolve_models(self, session: Any) -> tuple[Any, Any | None]:
        from sqlalchemy import select

        from app.models.model_config import ModelConfig

        worker = None
        if self.model_config_id:
            worker = await session.get(ModelConfig, self.model_config_id)
        else:
            worker = await session.scalar(
                select(ModelConfig)
                .where(
                    ModelConfig.enabled.is_(True),
                    ModelConfig.provider_type.in_(sorted(WORKER_PROVIDER_TYPES)),
                )
                .order_by(ModelConfig.created_at)
            )
        if worker is None or not worker.enabled:
            raise ExperimentExecutionError(
                "MODEL_CONFIG_REQUIRED",
                "No enabled Worker model configuration is available. "
                "Pass --model-config-id or create an enabled model config.",
            )
        if worker.provider_type not in WORKER_PROVIDER_TYPES:
            raise ExperimentExecutionError(
                "MODEL_PROVIDER_UNSUPPORTED",
                f"Worker model provider is unsupported: {worker.provider_type}",
            )

        reason = None
        if self.reason_model_config_id:
            reason = await session.get(ModelConfig, self.reason_model_config_id)
            if (
                reason is None
                or not reason.enabled
                or reason.provider_type not in OPENAI_WIRE_PROVIDER_TYPES
            ):
                raise ExperimentExecutionError(
                    "REASON_MODEL_CONFIG_INVALID",
                    "The configured Coordinator reason model is missing, disabled, "
                    "or does not speak the OpenAI wire protocol.",
                )
        else:
            candidates = list(
                (
                    await session.scalars(
                        select(ModelConfig)
                        .where(
                            ModelConfig.enabled.is_(True),
                            ModelConfig.provider_type.in_(
                                sorted(OPENAI_WIRE_PROVIDER_TYPES)
                            ),
                        )
                        .order_by(ModelConfig.created_at)
                    )
                ).all()
            )
            reason = next(
                (
                    candidate
                    for candidate in candidates
                    if "coordinator_reason"
                    in ((candidate.capabilities_json or {}).get("roles") or [])
                ),
                None,
            )
        return worker, reason

    async def _get_or_create_challenge(
        self,
        session: Any,
        *,
        request: ExperimentRequest,
    ) -> Any:
        from sqlalchemy import select

        from app.models.challenge import Challenge

        name = f"benchmark:{request.case_id}"
        challenge = await session.scalar(
            select(Challenge).where(Challenge.name == name).order_by(Challenge.created_at)
        )
        # The range endpoint is re-chosen per experiment, so a challenge found by
        # name may point at a port that no longer exists.  Returning it unchanged
        # would send the agent at a dead address and make every randomised
        # repetition fail for a reason that has nothing to do with the solver.
        resolved_target = resolve_case_target(request.case.target)
        parsed = urlparse(resolved_target)
        allowed_hosts = [parsed.hostname] if parsed.hostname else []
        # Kept in sync on reuse too: the cross-challenge lesson view (CAP-11)
        # groups challenges by these two fields, and a stale profile would
        # silently pool a case with the wrong technology stack.
        profile = {
            "vulnerability_type": request.case.vulnerability_type,
            "target_stack": list(request.case.target_stack),
        }
        if challenge is not None:
            changed = challenge.target_url != resolved_target
            if changed:
                challenge.target_url = resolved_target
                challenge.allowed_hosts = allowed_hosts
            metadata = dict(challenge.metadata_json or {})
            if any(metadata.get(key) != value for key, value in profile.items()):
                metadata.update(profile)
                challenge.metadata_json = metadata
                changed = True
            if changed:
                session.add(challenge)
                await session.flush()
            return challenge
        challenge = Challenge(
            name=name,
            description=(
                f"{request.case.vulnerability_type} benchmark case "
                f"for {request.case.case_id}."
            ),
            challenge_type="WEB_TARGET",
            target_url=resolved_target,
            allowed_hosts=allowed_hosts,
            source_path=str(request.case_path) if request.case_path else None,
            metadata_json={
                "benchmark_case_id": request.case_id,
                "benchmark_case_sha256": case_definition_hash(request.case),
                **profile,
                "initial_knowledge": list(request.case.initial_knowledge),
                "attack_surface": list(request.case.attack_surface),
            },
        )
        session.add(challenge)
        await session.flush()
        return challenge

    async def _create_run(
        self,
        session: Any,
        *,
        challenge: Any,
        worker: Any,
        reason: Any | None,
        request: ExperimentRequest,
    ) -> Any:
        from app.services.methodology_hints import hints_for_challenge
        from app.services.solver_state import solver_state_service
        from app.services.workspace import create_workspace
        from app.solver.muteki.runtime.configuration import (
            WorkerEngineSelection,
            write_runtime_selection,
        )

        hints = dict(hints_for_challenge(challenge))
        # This binding is per Run.  Executors never change process-global state.
        hints["knowledge_mode"] = request.arm
        hints = write_runtime_selection(
            hints,
            reason_model_config_id=str(reason.id) if reason is not None else None,
            worker_engines=[WorkerEngineSelection(worker.provider_type, str(worker.id))],
        )
        run = _solve_run_class()(
            challenge_id=str(challenge.id),
            engine_type=str(worker.provider_type),
            solver_mode=self.solver_mode,
            model_config_id=str(worker.id),
            role_snapshot_json={},
            hints_json=hints,
            workspace_path="pending",
            max_agent_steps=self.max_agent_steps,
            max_tool_calls=self.max_tool_calls,
            max_context_observations=self.max_context_observations,
            max_runtime_seconds=self.max_runtime_seconds,
            max_total_runtime_seconds=self.max_total_runtime_seconds,
        )
        session.add(run)
        await session.flush()
        run.workspace_path = str(create_workspace(str(run.id), challenge, []))
        await solver_state_service.initialize(
            session,
            run,
            str(challenge.challenge_type),
            [],
            str(challenge.name),
            str(challenge.description or ""),
        )
        return run


def _solve_run_class() -> Any:
    from app.models.run import SolveRun

    return SolveRun


def _model_snapshot(model: Any | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "id": str(model.id),
        "name": str(model.name),
        "provider_type": str(model.provider_type),
        "model_name": model.model_name,
        "enabled": bool(model.enabled),
        "capabilities": dict(model.capabilities_json or {}),
    }


async def _persist_evaluation_report(
    session: Any,
    run: Any,
    report: Mapping[str, Any],
) -> None:
    """Merge evaluator output into the Run without replacing solver output."""
    run.report_json = {
        **dict(run.report_json or {}),
        "evaluation_report": dict(report),
    }
    await session.commit()


def _execution_error_text(
    *,
    error_code: Any,
    error_message: str,
    status: str,
) -> str:
    parts = [part for part in (str(error_code or "").strip(), error_message.strip()) if part]
    if parts:
        return ": ".join(parts)
    return f"Run ended with non-success status: {status}"


__all__ = [
    "ARMS",
    "ARTIFACT_SCHEMA_VERSION",
    "BOOTSTRAP_ALPHA",
    "BOOTSTRAP_ITERATIONS",
    "BOOTSTRAP_SEED",
    "ExperimentConfig",
    "ExperimentConfigurationError",
    "ExperimentError",
    "ExperimentExecutionError",
    "ExperimentExecutor",
    "ExperimentOutputExistsError",
    "ExperimentRequest",
    "ExperimentRunner",
    "ExecutionResult",
    "MANIFEST_SCHEMA_VERSION",
    "MIN_PAIRED_SAMPLES",
    "RealRunExecutor",
    "build_summary",
    "case_definition_hash",
    "collect_runtime_metadata",
    "configuration_fingerprint",
    "is_successful",
    "load_raw_artifacts",
    "parse_arms",
]
