"""Read-only API endpoints for curated knowledge and experiment artifacts."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmark.evaluation import (
    EVALUATION_FINAL_RESULTS,
    EVALUATOR_NAME,
    EVALUATOR_SCHEMA_VERSION,
)
from app.benchmark.experiment import PROJECT_ROOT
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.knowledge.config import KnowledgeConfig, load_knowledge_config
from app.knowledge.loader import KnowledgeLoader
from app.knowledge.retriever import KnowledgeRetriever
from app.models.challenge import Challenge
from app.models.run import SolveRun

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

DEFAULT_KNOWLEDGE_EXPERIMENT_ROOT = PROJECT_ROOT / "data" / "experiments" / "knowledge"
EXPERIMENT_FILENAMES = {
    "manifest": "manifest.json",
    "plan": "plan.json",
    "summary": "summary.json",
}
MIN_PAIRED_SAMPLES = 10
_PUBLICATION_CUTOFF = date(2025, 1, 1)
_SAFE_EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_ARTIFACT_FILES = 100
_MAX_ARTIFACT_BYTES = 1_000_000
_MAX_JSON_DOCUMENT_BYTES = 5_000_000
_DB_AB_SCHEMA_VERSION = 1
_DB_AB_FACTOR = "knowledge_mode"
_DB_ARMS = frozenset({"off", "on"})
_DB_ROLE_TO_ARM = {"baseline": "off", "treatment": "on"}
_TERMINAL_RUN_STATUSES = frozenset(
    {
        "COMPLETED_SOLVED",
        "COMPLETED_UNSOLVED",
        "FAILED_ENGINE",
        "FAILED_TOOL",
        "FAILED_RUNNER",
        "TIMEOUT",
        "CANCELLED",
        "POLICY_BLOCKED",
    }
)


class KnowledgePreviewRequest(BaseModel):
    query: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=3, ge=1, le=100)


@router.get("/items")
def list_knowledge_items(
    query: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    trust_level: str | None = Query(default=None),
    target_stack: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items = _load_items()
    normalized_query = _normalized_filter(query)
    normalized_kind = _normalized_filter(kind)
    normalized_trust = _normalized_filter(trust_level)
    normalized_stack = _normalized_filter(target_stack)

    filtered = []
    for item in items:
        if normalized_query and not _matches_query(item, normalized_query):
            continue
        if normalized_kind and item.kind.casefold() != normalized_kind:
            continue
        if normalized_trust and item.trust_level.casefold() != normalized_trust:
            continue
        if normalized_stack and not any(
            normalized_stack in stack.casefold() for stack in item.target_stack
        ):
            continue
        filtered.append(item)

    filtered.sort(key=lambda item: item.id)
    page = filtered[offset : offset + limit]
    return {
        "data": {
            "items": [item.model_dump(mode="json") for item in page],
            "total": len(filtered),
            "limit": limit,
            "offset": offset,
        }
    }


@router.get("/stats")
def knowledge_stats() -> dict:
    items = _load_items()
    published_dates = sorted(
        item.published_at for item in items if item.published_at is not None
    )
    return {
        "data": {
            "total": len(items),
            "by_kind": dict(sorted(Counter(item.kind for item in items).items())),
            "by_trust_level": dict(
                sorted(Counter(item.trust_level for item in items).items())
            ),
            "published_before_2025_count": sum(
                item.published_at is not None and item.published_at < _PUBLICATION_CUTOFF
                for item in items
            ),
            "earliest_published_at": (
                published_dates[0].isoformat() if published_dates else None
            ),
            "latest_published_at": (
                published_dates[-1].isoformat() if published_dates else None
            ),
            "root": str(_knowledge_root()),
        }
    }


@router.post("/preview")
def preview_knowledge(payload: KnowledgePreviewRequest) -> dict:
    query = payload.query.strip()
    if not query:
        raise DomainError(
            "KNOWLEDGE_PREVIEW_QUERY_REQUIRED",
            "A non-empty query is required for knowledge preview.",
            status_code=400,
        )

    root = _knowledge_root()
    try:
        config = KnowledgeConfig(mode="on", root=root, top_k=payload.top_k)
        retriever = KnowledgeRetriever(config=config)
        results = retriever.retrieve(
            query=query,
            metadata=payload.metadata,
            top_k=payload.top_k,
        )
    except (OSError, ValueError) as error:
        raise _knowledge_unavailable(error) from error

    return {
        "data": {
            "results": [result.model_dump(mode="json") for result in results],
            "formatted_prior": retriever.format_prior(results),
            "query": query,
            "metadata": payload.metadata,
            "top_k": payload.top_k,
            "warning": (
                "Knowledge results are 待验证先验 hypotheses, not VerifiedFact evidence. "
                "Validate every item against observed target behavior."
            ),
        }
    }


@router.get("/experiments")
async def list_knowledge_experiments(
    session: AsyncSession = Depends(get_session),
) -> dict:
    root = _experiment_root()
    experiments: list[dict[str, Any]] = []
    if root.is_dir():
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            if (
                not _is_safe_experiment_id(directory.name)
                or directory.is_symlink()
                or not directory.is_dir()
            ):
                continue
            item = _read_experiment(directory, root=root)
            if item is not None:
                experiments.append(item)

    known_ids = {str(item["id"]) for item in experiments}
    try:
        db_experiments = await _read_db_experiment_pairs(session)
    except SQLAlchemyError as error:
        experiments.sort(key=lambda item: str(item["id"]))
        return {
            "data": {
                "experiments": experiments,
                "root": str(root),
                "db_error": {"error": type(error).__name__},
            }
        }
    experiments.extend(
        item for item in db_experiments if str(item["id"]) not in known_ids
    )
    experiments.sort(key=lambda item: str(item["id"]))

    return {"data": {"experiments": experiments, "root": str(root)}}


@router.get("/experiments/{experiment_id}")
async def get_knowledge_experiment(
    experiment_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    root = _experiment_root()
    if _is_safe_experiment_id(experiment_id):
        try:
            directory = _resolve_experiment_directory(root, experiment_id)
        except DomainError:
            directory = None
        if directory is not None:
            item = _read_experiment(directory, root=root)
            if item is not None:
                artifacts, artifacts_truncated = _read_artifacts(directory)
                item.update(
                    {
                        "artifacts": artifacts,
                        "artifact_count": len(artifacts),
                        "artifacts_truncated": artifacts_truncated,
                        "prior_policy": (
                            "Knowledge priors are hypotheses, not VerifiedFact evidence."
                        ),
                        "minimum_paired_samples": MIN_PAIRED_SAMPLES,
                    }
                )
                return {"data": item}

    if not _is_safe_experiment_id(experiment_id):
        raise _experiment_not_found(experiment_id)

    item = await _read_db_experiment_pair(session, experiment_id)
    if item is None:
        raise _experiment_not_found(experiment_id)

    return {"data": _with_experiment_detail_fields(item)}


def _knowledge_root() -> Path:
    try:
        return load_knowledge_config().root
    except ValueError as error:
        raise _knowledge_unavailable(error) from error


def _load_items():
    try:
        return KnowledgeLoader(_knowledge_root()).load()
    except (OSError, ValueError) as error:
        raise _knowledge_unavailable(error) from error


def _knowledge_unavailable(error: Exception) -> DomainError:
    return DomainError(
        "KNOWLEDGE_STORE_UNAVAILABLE",
        "The curated knowledge store could not be loaded.",
        details={"error": str(error)[:500]},
        status_code=503,
        retryable=True,
    )


def _experiment_root() -> Path:
    configured = os.getenv("C7F_KNOWLEDGE_EXPERIMENT_ROOT")
    if configured is None:
        return DEFAULT_KNOWLEDGE_EXPERIMENT_ROOT
    if not configured.strip():
        raise DomainError(
            "KNOWLEDGE_EXPERIMENT_ROOT_INVALID",
            "C7F_KNOWLEDGE_EXPERIMENT_ROOT must not be empty.",
            status_code=503,
            retryable=True,
        )
    try:
        return Path(configured).expanduser().resolve()
    except OSError as error:
        raise DomainError(
            "KNOWLEDGE_EXPERIMENT_ROOT_INVALID",
            "C7F_KNOWLEDGE_EXPERIMENT_ROOT could not be resolved.",
            details={"error": str(error)[:500]},
            status_code=503,
            retryable=True,
        ) from error


def _normalized_filter(value: str | None) -> str:
    return value.strip().casefold() if value else ""


def _matches_query(item: Any, query: str) -> bool:
    return any(
        query in value.casefold()
        for value in (item.id, item.title, item.technique, item.summary)
    )


def _is_safe_experiment_id(value: str) -> bool:
    return (
        bool(_SAFE_EXPERIMENT_ID.fullmatch(value))
        and value not in {".", ".."}
        and ".." not in value
    )


def _resolve_experiment_directory(root: Path, experiment_id: str) -> Path:
    if not _is_safe_experiment_id(experiment_id):
        raise _experiment_not_found(experiment_id)
    try:
        resolved_root = root.expanduser().resolve()
        directory = (resolved_root / experiment_id).resolve()
        if (
            directory.parent != resolved_root
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            raise _experiment_not_found(experiment_id)
        return directory
    except OSError as error:
        raise _experiment_not_found(experiment_id) from error


def _experiment_not_found(experiment_id: str) -> DomainError:
    return DomainError(
        "KNOWLEDGE_EXPERIMENT_NOT_FOUND",
        "Knowledge experiment not found.",
        details={"experiment_id": experiment_id[:128]},
        status_code=404,
    )


def _read_experiment(directory: Path, *, root: Path) -> dict[str, Any] | None:
    documents: dict[str, dict[str, Any] | None] = {}
    errors: dict[str, str] = {}
    for name, filename in EXPERIMENT_FILENAMES.items():
        document, error = _read_json_object(directory / filename)
        documents[name] = document
        if error is not None:
            errors[name] = error

    if not any(document is not None for document in documents.values()):
        return None

    manifest = documents["manifest"] or {}
    plan = documents["plan"] or {}
    summary = documents["summary"] or {}
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    case_ids = {
        str(task.get("case_id"))
        for task in tasks
        if isinstance(task, dict) and task.get("case_id") is not None
    }
    paired_effect = summary.get("paired_effect")
    if not isinstance(paired_effect, dict):
        paired_effect = {}

    status = "invalid"
    if documents["summary"] is not None:
        status = "completed"
    elif documents["plan"] is not None:
        status = "dry_run" if plan.get("dry_run") is True else "planned"
    elif documents["manifest"] is not None:
        status = "initialized"

    return {
        "id": directory.name,
        "source": "file",
        "path": str(directory.relative_to(root)),
        "created_at": manifest.get("created_at"),
        "started_at": plan.get("created_at") or manifest.get("created_at"),
        "status": status,
        "case_count": len(cases) or len(case_ids),
        "planned_jobs": len(tasks),
        "record_count": _non_negative_int(summary.get("record_count")),
        "paired_effect": paired_effect,
        "manifest": documents["manifest"],
        "plan": documents["plan"],
        "summary": documents["summary"],
        "read_errors": errors,
    }


async def _read_db_experiment_pairs(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    statement = select(
        SolveRun.id,
        SolveRun.challenge_id,
        SolveRun.status,
        SolveRun.hints_json,
        SolveRun.report_json,
        SolveRun.started_at,
        SolveRun.finished_at,
        SolveRun.created_at,
        SolveRun.updated_at,
        Challenge.metadata_json,
    ).join(Challenge, Challenge.id == SolveRun.challenge_id)
    rows = (await session.execute(statement)).all()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        candidate = _db_pair_candidate(row)
        if candidate is not None:
            groups[candidate["experiment_id"]].append(candidate)

    experiments = []
    for experiment_id, runs in groups.items():
        item = _complete_db_pair(experiment_id, runs)
        if item is not None:
            experiments.append(item)
    return sorted(experiments, key=lambda item: str(item["id"]))


async def _read_db_experiment_pair(
    session: AsyncSession,
    experiment_id: str,
) -> dict[str, Any] | None:
    experiments = await _read_db_experiment_pairs(session)
    return next(
        (item for item in experiments if item["id"] == experiment_id),
        None,
    )


def _db_pair_candidate(row: Any) -> dict[str, Any] | None:
    hints = _row_value(row, "hints_json", 3)
    report_json = _row_value(row, "report_json", 4)
    if not isinstance(hints, Mapping):
        return None
    ab_experiment = hints.get("ab_experiment")
    if not isinstance(ab_experiment, Mapping):
        return None
    schema_version = ab_experiment.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version != _DB_AB_SCHEMA_VERSION
        or ab_experiment.get("factor") != _DB_AB_FACTOR
    ):
        return None

    experiment_id = ab_experiment.get("experiment_id")
    if not isinstance(experiment_id, str) or not _is_safe_experiment_id(
        experiment_id
    ):
        return None

    role = ab_experiment.get("role")
    if role not in _DB_ROLE_TO_ARM:
        return None
    if (
        ab_experiment.get("baseline_value") != "off"
        or ab_experiment.get("treatment_value") != "on"
    ):
        return None

    hint_mode = hints.get("knowledge_mode")
    if hint_mode not in _DB_ARMS or _DB_ROLE_TO_ARM[role] != hint_mode:
        return None
    observed_mode = (
        ab_experiment.get("arm")
        or ab_experiment.get("knowledge_mode")
        or ab_experiment.get("value")
    )
    if observed_mode is not None and observed_mode != hint_mode:
        return None

    run_id = str(_row_value(row, "id", 0) or "").strip()
    source_run_id = str(ab_experiment.get("source_run_id") or "").strip()
    paired_run_id = str(ab_experiment.get("paired_run_id") or "").strip()
    if (
        not run_id
        or not source_run_id
        or not paired_run_id
        or paired_run_id == run_id
    ):
        return None

    challenge_metadata = _row_value(row, "metadata_json", 9)
    benchmark_case_id = (
        challenge_metadata.get("benchmark_case_id")
        if isinstance(challenge_metadata, Mapping)
        else None
    )
    evaluation_report = _extract_evaluation_report(
        run_id=run_id,
        report_json=report_json if isinstance(report_json, Mapping) else {},
        expected_case_id=(
            benchmark_case_id
            if isinstance(benchmark_case_id, str) and benchmark_case_id.strip()
            else None
        ),
    )
    final_result = None
    if isinstance(evaluation_report, Mapping):
        reported_result = evaluation_report.get("final_result")
        if isinstance(reported_result, str) and (
            reported_result in EVALUATION_FINAL_RESULTS
        ):
            final_result = reported_result

    return {
        "experiment_id": experiment_id,
        "run_id": run_id,
        "challenge_id": _optional_text(_row_value(row, "challenge_id", 1)),
        "terminal_status": str(_row_value(row, "status", 2) or "UNKNOWN"),
        "knowledge_mode": hint_mode,
        "role": role,
        "source_run_id": source_run_id,
        "paired_run_id": paired_run_id,
        "evaluation_report": (
            dict(evaluation_report)
            if isinstance(evaluation_report, Mapping)
            else None
        ),
        "final_result": final_result,
        "evaluator_backed": final_result is not None,
        "success": final_result == "SUCCESS" if final_result is not None else None,
        "started_at": _json_time(_row_value(row, "started_at", 5)),
        "finished_at": _json_time(_row_value(row, "finished_at", 6)),
        "created_at": _json_time(_row_value(row, "created_at", 7)),
        "updated_at": _json_time(_row_value(row, "updated_at", 8)),
        "ab_experiment": dict(ab_experiment),
    }


def _complete_db_pair(
    experiment_id: str,
    runs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if len(runs) != 2:
        return None
    by_arm: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}
    for run in runs:
        arm = run["knowledge_mode"]
        role = run["role"]
        if arm in by_arm or role in by_role:
            return None
        by_arm[arm] = run
        by_role[role] = run
    if set(by_arm) != _DB_ARMS:
        return None
    if set(by_role) != set(_DB_ROLE_TO_ARM):
        return None
    if any(
        by_arm[arm] is not by_role[role]
        for role, arm in _DB_ROLE_TO_ARM.items()
    ):
        return None
    if any(
        run["terminal_status"] not in _TERMINAL_RUN_STATUSES for run in runs
    ):
        return None
    run_ids = {run["run_id"] for run in runs}
    source_run_ids = {run["source_run_id"] for run in runs}
    if len(source_run_ids) != 1 or not source_run_ids.issubset(run_ids):
        return None
    for run in runs:
        paired_run_id = run["paired_run_id"]
        if paired_run_id not in run_ids or paired_run_id == run["run_id"]:
            return None
        if run["run_id"] == by_arm["off"]["run_id"]:
            expected_pair = by_arm["on"]["run_id"]
        else:
            expected_pair = by_arm["off"]["run_id"]
        if paired_run_id != expected_pair:
            return None
    challenge_ids = {
        run["challenge_id"]
        for run in runs
        if run["challenge_id"] is not None
    }
    if len(challenge_ids) != 1:
        return None

    ordered_runs = [by_arm["off"], by_arm["on"]]
    arms = [_db_run_evidence(run) for run in ordered_runs]
    summary = _db_pair_summary(arms)
    manifest = {
        "source": "db_pair",
        "schema_version": _DB_AB_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "factor": _DB_AB_FACTOR,
        "baseline_value": "off",
        "treatment_value": "on",
        "baseline_run_id": by_arm["off"]["run_id"],
        "treatment_run_id": by_arm["on"]["run_id"],
        "created_at": min(
            (
                run["created_at"]
                for run in ordered_runs
                if run["created_at"] is not None
            ),
            default=None,
        ),
    }
    return {
        "id": experiment_id,
        "source": "db_pair",
        "path": f"db_pair/{experiment_id}",
        "created_at": manifest["created_at"],
        "started_at": min(
            (
                run["started_at"]
                for run in ordered_runs
                if run["started_at"] is not None
            ),
            default=None,
        ),
        "status": "completed",
        "case_count": len(
            {
                run["challenge_id"]
                for run in ordered_runs
                if run["challenge_id"] is not None
            }
        )
        or 1,
        "planned_jobs": 2,
        "record_count": 2,
        "paired_effect": summary["paired_effect"],
        "factor": _DB_AB_FACTOR,
        "baseline_value": "off",
        "treatment_value": "on",
        "baseline_run_id": by_arm["off"]["run_id"],
        "treatment_run_id": by_arm["on"]["run_id"],
        "manifest": manifest,
        "plan": {
            "source": "db_pair",
            "tasks": [
                {"run_id": run["run_id"], "arm": run["knowledge_mode"]}
                for run in ordered_runs
            ],
        },
        "summary": summary,
        "read_errors": {},
        "arms": arms,
    }


def _db_pair_summary(arms: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm = {
        arm["knowledge_mode"]: _db_arm_summary([arm])
        for arm in arms
    }
    evaluator_backed = all(arm["evaluator_backed"] for arm in arms)
    off_success = int(by_arm["off"]["success"] is True)
    on_success = int(by_arm["on"]["success"] is True)
    paired_effect = {
        "sample_count": 1,
        "pairs": 1,
        "minimum_recommended_samples": MIN_PAIRED_SAMPLES,
        "sample_adequate": False,
        "claim": "descriptive_only",
        "off_success_count": off_success,
        "on_success_count": on_success,
        "success_delta": (
            float(on_success - off_success) if evaluator_backed else None
        ),
        "definition": (
            "Evaluator-backed success rate difference (on - off)."
        ),
        "warning": (
            "descriptive_only: one symmetric Run pair is insufficient for an "
            "effect or generalization claim."
        ),
    }
    return {
        "source": "db_pair",
        "record_count": len(arms),
        "success_definition": (
            "evaluation_report.final_result == 'SUCCESS'; Run status alone is "
            "not success"
        ),
        "by_arm": by_arm,
        "paired_effect": paired_effect,
    }


def _db_arm_summary(arms: list[dict[str, Any]]) -> dict[str, Any]:
    success_count = sum(arm["success"] is True for arm in arms)
    error_count = sum(
        _is_error_run_status(arm["terminal_status"]) for arm in arms
    )
    return {
        "attempts": len(arms),
        "success": success_count,
        "errors": error_count,
        "evaluated_attempts": sum(arm["evaluator_backed"] for arm in arms),
        "final_status_distribution": dict(
            sorted(Counter(arm["terminal_status"] for arm in arms).items())
        ),
        "average_duration_seconds": None,
        "average_tool_calls": None,
    }


def _db_run_evidence(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "baseline" if run["knowledge_mode"] == "off" else "treatment",
        "arm": run["knowledge_mode"],
        "knowledge_mode": run["knowledge_mode"],
        "run_id": run["run_id"],
        "terminal_status": run["terminal_status"],
        "status": run["terminal_status"],
        "evaluation_report": run["evaluation_report"],
        "final_result": run["final_result"],
        "success": run["success"],
        "evaluator_backed": run["evaluator_backed"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "ab_experiment": run["ab_experiment"],
    }


def _extract_evaluation_report(
    *,
    run_id: str,
    report_json: Mapping[str, Any],
    expected_case_id: str | None,
) -> Mapping[str, Any] | None:
    report = report_json.get("evaluation_report")
    if not isinstance(report, Mapping):
        return None
    if report.get("run_id") != run_id:
        return None
    final_result = report.get("final_result")
    if (
        not isinstance(final_result, str)
        or final_result not in EVALUATION_FINAL_RESULTS
    ):
        return None
    case_id = report.get("case_id")
    metrics = report.get("metrics")
    if not isinstance(case_id, str) or not case_id.strip():
        return None
    if case_id != expected_case_id:
        return None
    if not isinstance(metrics, Mapping):
        return None
    evaluator = report.get("evaluator")
    if not isinstance(evaluator, Mapping):
        return None
    if evaluator.get("name") != EVALUATOR_NAME:
        return None
    schema_version = evaluator.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != EVALUATOR_SCHEMA_VERSION
    ):
        return None
    if evaluator.get("run_id") != run_id:
        return None
    if evaluator.get("basis") != "durable_run_evidence":
        return None
    return report


def _with_experiment_detail_fields(item: dict[str, Any]) -> dict[str, Any]:
    item.update(
        {
            "artifacts": [],
            "artifact_count": 0,
            "artifacts_truncated": False,
            "prior_policy": (
                "Knowledge priors are hypotheses, not VerifiedFact evidence."
            ),
            "minimum_paired_samples": MIN_PAIRED_SAMPLES,
        }
    )
    return item


def _row_value(row: Any, name: str, index: int) -> Any:
    value = getattr(row, name, None)
    if value is not None:
        return value
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


def _json_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _is_error_run_status(status: str) -> bool:
    return status.startswith("FAILED") or status in {
        "TIMEOUT",
        "CANCELLED",
        "POLICY_BLOCKED",
    }


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if path.is_symlink():
        return None, "symlink_not_allowed"
    if not path.exists():
        return None, None
    try:
        if not path.is_file():
            return None, "not_a_file"
        if path.stat().st_size > _MAX_JSON_DOCUMENT_BYTES:
            return None, "file_too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, type(error).__name__
    if not isinstance(payload, dict):
        return None, "expected_json_object"
    return payload, None


def _read_artifacts(experiment_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    artifact_dir = experiment_dir / "artifacts"
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        return [], False

    candidates = sorted(
        (
            path
            for path in artifact_dir.glob("*.json")
            if not path.is_symlink() and path.is_file()
        ),
        key=lambda path: path.name,
    )
    selected = candidates[:_MAX_ARTIFACT_FILES]
    artifacts: list[dict[str, Any]] = []
    for path in selected:
        size_bytes = path.stat().st_size
        item: dict[str, Any] = {
            "name": path.name,
            "path": path.relative_to(experiment_dir).as_posix(),
            "size_bytes": size_bytes,
        }
        if size_bytes > _MAX_ARTIFACT_BYTES:
            item.update({"omitted": True, "reason": "file_too_large", "data": None})
        else:
            data, error = _read_json_object(path)
            item.update({"omitted": False, "data": data})
            if error is not None:
                item["error"] = error
        artifacts.append(item)
    return artifacts, len(candidates) > _MAX_ARTIFACT_FILES


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
