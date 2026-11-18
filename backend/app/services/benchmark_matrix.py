"""Launch and track a benchmark matrix as an independent child process.

``ExperimentRunner`` takes hours and must survive a backend restart, so the API
spawns it as its own process and follows it through files on disk instead of an
``asyncio.Task``.  A task would die with the server and leave a half-finished
matrix with no owner (todo §11.1); a child process keeps running, and if the
server does restart, the state file is enough to tell "still running" apart
from "gone without a summary" — the second is recoverable with
``python -m app.benchmark --resume --output-dir <same dir>``.

Nothing here recomputes statistics: ``summary.json`` written by the runner is
the single source of truth, and this module only reports progress around it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from app.benchmark.experiment import (
    ARMS,
    ARTIFACT_DIRNAME,
    BACKEND_ROOT,
    PLAN_FILENAME,
    PROJECT_ROOT,
    SUMMARY_FILENAME,
    _process_is_running,
    parse_arms,
)
from app.core.exceptions import DomainError

EXPERIMENTS_ROOT = PROJECT_ROOT / "backend" / "data" / "experiments"
DEFAULT_CASES_DIR = PROJECT_ROOT / "backend" / "app" / "benchmark" / "cases"

STATE_FILENAME = "state.json"
REQUEST_FILENAME = "run_request.json"
LOG_FILENAME = "matrix.log"

RANDOMIZE_SCOPES = ("never", "run", "repeat")
EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_REPEATS = 50

STATUS_RUNNING = "RUNNING"
STATUS_FINISHED = "FINISHED"
STATUS_INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class MatrixRequest:
    """A validated matrix launch request."""

    experiment_id: str
    case_dir: Path
    output_dir: Path
    arms: tuple[str, ...] = ARMS
    repeats: int = 1
    randomize_range: str = "never"
    worker_model: str | None = None
    reason_model: str | None = None
    resume: bool = False
    dry_run: bool = False
    runtime_selection: Mapping[str, Any] = field(default_factory=dict)

    def argv(self) -> list[str]:
        arguments = [
            sys.executable,
            "-m",
            "app.benchmark",
            "--case-dir",
            str(self.case_dir),
            "--output-dir",
            str(self.output_dir),
            "--repeats",
            str(self.repeats),
            "--arms",
            ",".join(self.arms),
            "--randomize-range",
            self.randomize_range,
            "--json",
        ]
        if self.resume:
            arguments.append("--resume")
        if self.dry_run:
            arguments.append("--dry-run")
        if self.worker_model:
            arguments.extend(["--worker-model", self.worker_model])
        if self.reason_model:
            arguments.extend(["--reason-model", self.reason_model])
        return arguments


def build_request(payload: Mapping[str, Any]) -> MatrixRequest:
    """Validate an API payload into a launch request, or raise ``DomainError``."""

    raw_id = str(payload.get("experiment_id") or "").strip()
    if not raw_id:
        raw_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not EXPERIMENT_ID.match(raw_id):
        raise DomainError(
            "MATRIX_EXPERIMENT_ID_INVALID",
            "experiment_id must be a short [A-Za-z0-9._-] token.",
            {"experiment_id": raw_id},
            422,
        )

    output_dir = _resolve(payload.get("output_dir") or EXPERIMENTS_ROOT / raw_id)
    if not _within(output_dir, EXPERIMENTS_ROOT):
        # A matrix writes artefacts and a log into its output directory; letting
        # the caller point that anywhere would turn one API call into arbitrary
        # filesystem writes.
        raise DomainError(
            "MATRIX_OUTPUT_DIR_NOT_ALLOWED",
            f"output_dir must stay inside {EXPERIMENTS_ROOT}.",
            {"output_dir": str(output_dir), "allowed_root": str(EXPERIMENTS_ROOT)},
            422,
        )

    case_dir = _resolve(payload.get("case_dir") or DEFAULT_CASES_DIR)
    if not _within(case_dir, PROJECT_ROOT):
        raise DomainError(
            "MATRIX_CASE_DIR_NOT_ALLOWED",
            f"case_dir must stay inside {PROJECT_ROOT}.",
            {"case_dir": str(case_dir)},
            422,
        )
    if not case_dir.is_dir() or not sorted(case_dir.glob("*.json")):
        raise DomainError(
            "MATRIX_CASE_DIR_EMPTY",
            "case_dir must exist and contain at least one *.json case.",
            {"case_dir": str(case_dir)},
            422,
        )

    arms = _arms(payload.get("arms"))
    repeats = _int(payload.get("repeats"), default=1, name="repeats")
    if not 1 <= repeats <= MAX_REPEATS:
        raise DomainError(
            "MATRIX_REPEATS_OUT_OF_RANGE",
            f"repeats must be between 1 and {MAX_REPEATS}.",
            {"repeats": repeats},
            422,
        )

    scope = str(payload.get("randomize_range") or "never")
    if scope not in RANDOMIZE_SCOPES:
        raise DomainError(
            "MATRIX_RANDOMIZE_SCOPE_INVALID",
            f"randomize_range must be one of {', '.join(RANDOMIZE_SCOPES)}.",
            {"randomize_range": scope},
            422,
        )

    return MatrixRequest(
        experiment_id=raw_id,
        case_dir=case_dir,
        output_dir=output_dir,
        arms=arms,
        repeats=repeats,
        randomize_range=scope,
        worker_model=_text(payload.get("worker_model_config_id") or payload.get("worker_model")),
        reason_model=_text(payload.get("reason_model_config_id") or payload.get("reason_model")),
        resume=bool(payload.get("resume")),
        dry_run=bool(payload.get("dry_run")),
    )


def start(request: MatrixRequest) -> dict[str, Any]:
    """Spawn the matrix process and return its initial state."""

    if request.output_dir.exists() and any(request.output_dir.iterdir()) and not request.resume:
        raise DomainError(
            "MATRIX_OUTPUT_DIR_NOT_EMPTY",
            "output_dir already has artefacts; pass resume=true to continue it.",
            {"output_dir": str(request.output_dir)},
            409,
        )
    state_path = request.output_dir / STATE_FILENAME
    if state_path.is_file() and _state_status(read_json(state_path)) == STATUS_RUNNING:
        raise DomainError(
            "MATRIX_ALREADY_RUNNING",
            "A matrix is already running in this output directory.",
            {"output_dir": str(request.output_dir)},
            409,
        )

    request.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(request.output_dir / REQUEST_FILENAME, _request_document(request))

    log_path = request.output_dir / LOG_FILENAME
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(BACKEND_ROOT)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            request.argv(),
            cwd=str(BACKEND_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            # Detach so the matrix outlives the API server it was started from.
            start_new_session=os.name != "nt",
        )

    state = {
        "experiment_id": request.experiment_id,
        "pid": int(process.pid),
        "started_at": datetime.now(UTC).isoformat(),
        "argv": request.argv(),
        "output_dir": str(request.output_dir),
        "case_dir": str(request.case_dir),
        "arms": list(request.arms),
        "repeats": request.repeats,
        "randomize_range": request.randomize_range,
        "log": str(log_path),
    }
    write_json(state_path, state)
    return read_experiment(request.experiment_id, request.output_dir)


def read_experiment(experiment_id: str, output_dir: Path | None = None) -> dict[str, Any]:
    """State plus progress for one matrix."""

    directory = output_dir or _experiment_dir(experiment_id)
    state = read_json(directory / STATE_FILENAME)
    status = _state_status(state)
    summary = read_json(directory / SUMMARY_FILENAME)
    plan = read_json(directory / PLAN_FILENAME)
    planned = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    completed = len(list((directory / ARTIFACT_DIRNAME).glob("*.json"))) if (directory / ARTIFACT_DIRNAME).is_dir() else 0
    return {
        "experiment_id": experiment_id,
        "status": status,
        "output_dir": str(directory),
        "pid": state.get("pid"),
        "started_at": state.get("started_at"),
        "arms": state.get("arms") or [],
        "repeats": state.get("repeats"),
        "randomize_range": state.get("randomize_range"),
        "log": state.get("log"),
        "planned_tasks": len(planned),
        "completed_tasks": completed,
        "summary": summary or None,
        "recover_with": (
            f"python -m app.benchmark --resume --output-dir {directory}"
            if status == STATUS_INTERRUPTED
            else None
        ),
    }


def list_experiments() -> list[dict[str, Any]]:
    """Every matrix directory that has a state file, newest first."""

    if not EXPERIMENTS_ROOT.is_dir():
        return []
    found = [
        read_experiment(directory.name, directory)
        for directory in sorted(EXPERIMENTS_ROOT.iterdir(), reverse=True)
        if directory.is_dir() and (directory / STATE_FILENAME).is_file()
    ]
    return found


def _state_status(state: Mapping[str, Any]) -> str:
    if not state:
        return "UNKNOWN"
    if (Path(str(state.get("output_dir") or "")) / SUMMARY_FILENAME).is_file():
        return STATUS_FINISHED
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        return STATUS_INTERRUPTED
    return STATUS_RUNNING if _process_is_running(pid) else STATUS_INTERRUPTED


def _request_document(request: MatrixRequest) -> dict[str, Any]:
    return {
        "experiment_id": request.experiment_id,
        "case_dir": str(request.case_dir),
        "output_dir": str(request.output_dir),
        "arms": list(request.arms),
        "repeats": request.repeats,
        "randomize_range": request.randomize_range,
        "worker_model_config_id": request.worker_model,
        "reason_model_config_id": request.reason_model,
        "resume": request.resume,
        "dry_run": request.dry_run,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _experiment_dir(experiment_id: str) -> Path:
    if not EXPERIMENT_ID.match(str(experiment_id or "")):
        raise DomainError(
            "MATRIX_EXPERIMENT_ID_INVALID",
            "experiment_id must be a short [A-Za-z0-9._-] token.",
            {"experiment_id": experiment_id},
            422,
        )
    directory = EXPERIMENTS_ROOT / str(experiment_id)
    if not directory.is_dir():
        raise DomainError(
            "MATRIX_NOT_FOUND",
            "No matrix with that experiment_id.",
            {"experiment_id": experiment_id},
            404,
        )
    return directory


def _resolve(value: Any) -> Path:
    return Path(str(value)).expanduser().resolve()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def _arms(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ARMS
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (list, tuple)):
        raw = ",".join(str(item) for item in value)
    else:
        raise DomainError("MATRIX_ARMS_INVALID", "arms must be a string or a list.", {"arms": value}, 422)
    try:
        arms = parse_arms(raw)
    except ValueError as error:
        raise DomainError("MATRIX_ARMS_INVALID", str(error), {"arms": value}, 422) from error
    if not arms:
        raise DomainError("MATRIX_ARMS_INVALID", "arms must select at least one arm.", {"arms": value}, 422)
    return arms


def _int(value: Any, *, default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise DomainError(
            "MATRIX_ARGUMENT_INVALID",
            f"{name} must be an integer.",
            {name: value},
            422,
        ) from error


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "EXPERIMENTS_ROOT",
    "MatrixRequest",
    "build_request",
    "list_experiments",
    "read_experiment",
    "start",
]
