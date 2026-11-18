"""Create and pair control Runs for run-scoped experiments."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.knowledge.config import load_knowledge_config, resolve_knowledge_mode
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.models.skill import RunSkillSnapshot
from app.models.solver_state import SolverState
from app.orchestration.state_machine import TERMINAL, RunStatus
from app.schemas.run import (
    RunControlExperimentCreate,
    RunCreate,
)
from app.schemas.run import (
    WorkerEngineSelection as RunWorkerEngineSelection,
)
from app.solver.muteki.runtime.configuration import (
    runtime_selection_from_hints,
)

CONTROL_FACTOR = "knowledge_mode"
BASELINE_VALUE = "off"
TREATMENT_VALUE = "on"
SUPPORTED_VALUES = frozenset({BASELINE_VALUE, TREATMENT_VALUE})
AB_EXPERIMENT_SCHEMA_VERSION = 1
ACTIVE_EXECUTION_STATUSES = frozenset(
    {
        RunStatus.RUNNING,
        RunStatus.PREPARING,
        RunStatus.ANALYZING,
        RunStatus.PLANNING,
        RunStatus.EXECUTING,
        RunStatus.EVALUATING,
        RunStatus.VERIFYING_FLAG,
        RunStatus.REPORTING,
        RunStatus.RETRYING,
    }
)

_TRANSIENT_HINT_KEYS = frozenset(
    {
        "ab_experiment",
        "evaluation_report",
        "muteki_board_semantic",
        "muteki_title",
        "report_json",
    }
)
_COPIED_SCALAR_FIELDS = (
    "engine_type",
    "solver_mode",
    "model_config_id",
    "role_name",
    "role_version",
    "role_snapshot_json",
    "controller_revision",
    "max_agent_steps",
    "max_tool_calls",
    "max_context_observations",
    "max_runtime_seconds",
    "max_total_runtime_seconds",
    "agent_checkpoint_interval",
    "assistance_level",
    "conversation_summary",
)
_PAIRING_LOCKS: dict[tuple[Any, str], asyncio.Lock] = {}

CreateRunFn = Callable[
    [str, RunCreate, AsyncSession],
    Awaitable[dict[str, Any]],
]
StartRunFn = Callable[[str, AsyncSession], Awaitable[dict[str, Any]]]
ReadRunFn = Callable[[SolveRun], Any]


async def create_control_experiment(
    session: AsyncSession,
    source_run: SolveRun,
    payload: RunControlExperimentCreate,
    *,
    create_run_fn: CreateRunFn,
    start_run_fn: StartRunFn,
    read_run_fn: ReadRunFn,
) -> dict[str, Any]:
    """Create a paired control Run and optionally start only that new Run."""

    factor = str(getattr(payload, "factor", "") or "").strip()
    if factor != CONTROL_FACTOR:
        raise DomainError(
            "CONTROL_EXPERIMENT_FACTOR_UNSUPPORTED",
            "Only knowledge_mode is supported for run control experiments.",
            {"factor": factor, "supported_factors": [CONTROL_FACTOR]},
            status_code=422,
        )

    counterfactual_value = str(
        getattr(payload, "counterfactual_value", "") or ""
    ).strip()
    if counterfactual_value not in SUPPORTED_VALUES:
        raise DomainError(
            "CONTROL_EXPERIMENT_VALUE_INVALID",
            "counterfactual_value must be off or on.",
            {
                "counterfactual_value": counterfactual_value,
                "supported_values": sorted(SUPPORTED_VALUES),
            },
            status_code=422,
        )

    async with _pairing_lock(source_run.id):
        # A concurrent request may have completed while this request waited.
        # Refresh both the source row and the session state before deciding.
        await session.refresh(source_run)
        _ensure_source_control_eligible(source_run)
        await _ensure_source_challenge_is_evaluatable(session, source_run)

        source_hints = _hints(source_run)
        source_value = _source_knowledge_mode(source_hints)
        expected_value = (
            TREATMENT_VALUE if source_value == BASELINE_VALUE else BASELINE_VALUE
        )
        if counterfactual_value != expected_value:
            raise DomainError(
                "CONTROL_EXPERIMENT_VALUE_MISMATCH",
                "counterfactual_value must be the opposite of the source Run value.",
                {
                    "source_value": source_value,
                    "counterfactual_value": counterfactual_value,
                },
                status_code=422,
            )

        await _reject_existing_pairing(session, source_run)

        experiment_id = str(uuid4())
        create_payload = await _build_control_run_create(
            session,
            source_run,
        )
        created = await create_run_fn(
            source_run.challenge_id,
            create_payload,
            session,
        )
        control_id = _created_run_id(created)
        control_run = await session.get(SolveRun, control_id)
        if control_run is None:
            raise DomainError(
                "CONTROL_RUN_CREATE_FAILED",
                "The existing Run creation flow did not persist the control Run.",
                {"control_run_id": control_id},
                status_code=500,
            )
        control_run.control_source_run_id = source_run.id
        await session.flush()

        _copy_source_configuration(source_run, control_run)
        active_skill_ids = await _clone_skill_snapshots(
            session,
            source_run.id,
            control_run.id,
        )
        await _sync_active_skill_ids(session, control_run.id, active_skill_ids)

        control_hints = _persistent_hints(source_hints)
        control_hints["knowledge_mode"] = counterfactual_value

        source_role = (
            "baseline" if source_value == BASELINE_VALUE else "treatment"
        )
        control_role = (
            "baseline" if counterfactual_value == BASELINE_VALUE else "treatment"
        )
        source_metadata = {
            "schema_version": AB_EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "factor": CONTROL_FACTOR,
            "role": source_role,
            "baseline_value": BASELINE_VALUE,
            "treatment_value": TREATMENT_VALUE,
            "source_run_id": source_run.id,
            "paired_run_id": control_run.id,
        }
        control_metadata = {
            "schema_version": AB_EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "factor": CONTROL_FACTOR,
            "role": control_role,
            "baseline_value": BASELINE_VALUE,
            "treatment_value": TREATMENT_VALUE,
            "source_run_id": source_run.id,
            "paired_run_id": source_run.id,
        }

        source_hints = dict(source_hints)
        source_hints["knowledge_mode"] = source_value
        source_hints["ab_experiment"] = source_metadata
        control_hints["ab_experiment"] = control_metadata
        source_run.hints_json = source_hints
        control_run.hints_json = control_hints
        await session.commit()

    started_run_ids: list[str] = []
    start_error: dict[str, Any] | None = None
    if bool(getattr(payload, "start", False)):
        if control_run.status == "CREATED":
            try:
                await start_run_fn(control_run.id, session)
            except DomainError as error:
                await _rollback(session)
                start_error = {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            except Exception as error:  # noqa: BLE001
                await _rollback(session)
                start_error = {
                    "code": "CONTROL_RUN_START_FAILED",
                    "message": "The control Run was created but could not be started.",
                    "details": {"error": str(error)},
                }
            else:
                started_run_ids.append(control_run.id)
        else:
            start_error = {
                "code": "CONTROL_RUN_NOT_PENDING",
                "message": "Only a newly created pending control Run can be started.",
                "details": {"status": control_run.status},
            }

    return {
        "data": {
            "experiment_id": experiment_id,
            "source_run": read_run_fn(source_run),
            "control_run": read_run_fn(control_run),
            "started_run_ids": started_run_ids,
            "start_error": start_error,
        }
    }


async def _reject_existing_pairing(
    session: AsyncSession,
    source_run: SolveRun,
) -> None:
    control_id = await session.scalar(
        select(SolveRun.id).where(SolveRun.control_source_run_id == source_run.id)
    )
    if control_id:
        raise DomainError(
            "RUN_ALREADY_IN_CONTROL_EXPERIMENT",
            "The source Run already has a control Run.",
            {
                "source_run_id": source_run.id,
                "paired_run_id": control_id,
            },
            status_code=409,
        )

    source_metadata = _experiment_metadata(source_run)
    if source_metadata is not None:
        paired_run_id = str(source_metadata.get("paired_run_id") or "").strip()
        code = (
            "CONTROL_EXPERIMENT_SELF_PAIR"
            if paired_run_id == source_run.id
            else "RUN_ALREADY_IN_CONTROL_EXPERIMENT"
        )
        raise DomainError(
            code,
            "The source Run is already part of a control experiment.",
            {
                "source_run_id": source_run.id,
                "paired_run_id": paired_run_id or None,
                "experiment_id": source_metadata.get("experiment_id"),
            },
            status_code=409,
        )

    candidates = list((await session.scalars(select(SolveRun))).all())
    for candidate in candidates:
        if candidate.id == source_run.id:
            continue
        metadata = _experiment_metadata(candidate)
        if metadata is None:
            continue
        if str(metadata.get("paired_run_id") or "").strip() == source_run.id:
            raise DomainError(
                "RUN_ALREADY_IN_CONTROL_EXPERIMENT",
                "Another Run already pairs this source Run.",
                {
                    "source_run_id": source_run.id,
                    "paired_run_id": candidate.id,
                    "experiment_id": metadata.get("experiment_id"),
                },
                status_code=409,
            )


async def _build_control_run_create(
    session: AsyncSession,
    source_run: SolveRun,
) -> RunCreate:
    source_hints = _hints(source_run)
    reason_model_config_id, runtime_workers = runtime_selection_from_hints(
        source_hints,
        fallback_engine_type=source_run.engine_type,
        fallback_model_config_id=source_run.model_config_id,
    )
    worker_engines = [
        RunWorkerEngineSelection(
            engine_type=item.engine_type,
            model_config_id=item.model_config_id,
        )
        for item in runtime_workers
    ]

    # RunCreate intentionally exposes only current public modes.  An existing
    # legacy Run must still be cloned without changing its persisted mode.
    return RunCreate.model_construct(
        engine_type=source_run.engine_type,
        solver_mode=source_run.solver_mode,
        model_config_id=source_run.model_config_id,
        reason_model_config_id=reason_model_config_id,
        worker_engines=worker_engines,
        max_agent_steps=source_run.max_agent_steps,
        max_tool_calls=source_run.max_tool_calls,
        max_context_observations=source_run.max_context_observations,
        max_runtime_seconds=source_run.max_runtime_seconds,
        max_total_runtime_seconds=source_run.max_total_runtime_seconds,
        # Skill snapshots are cloned verbatim after the generic create flow.
        # Passing the source IDs here would resolve mutable Skill rows and
        # could silently change the Skill content or version.
        selected_skill_ids=[],
        disabled_skill_ids=[],
        conversation_id=None,
    )


async def _clone_skill_snapshots(
    session: AsyncSession,
    source_run_id: str,
    control_run_id: str,
) -> list[str]:
    snapshots = list(
        (
            await session.scalars(
                select(RunSkillSnapshot)
                .where(RunSkillSnapshot.run_id == source_run_id)
                .order_by(RunSkillSnapshot.priority, RunSkillSnapshot.created_at)
            )
        ).all()
    )
    await session.execute(
        delete(RunSkillSnapshot).where(
            RunSkillSnapshot.run_id == control_run_id
        )
    )
    for snapshot in snapshots:
        session.add(
            RunSkillSnapshot(
                run_id=control_run_id,
                skill_id=snapshot.skill_id,
                skill_name=snapshot.skill_name,
                skill_version=snapshot.skill_version,
                content_snapshot=snapshot.content_snapshot,
                allowed_tools_snapshot=list(
                    snapshot.allowed_tools_snapshot or []
                ),
                config_snapshot=dict(snapshot.config_snapshot or {}),
                priority=snapshot.priority,
            )
        )
    await session.flush()
    return [snapshot.skill_id for snapshot in snapshots]


async def _sync_active_skill_ids(
    session: AsyncSession,
    control_run_id: str,
    active_skill_ids: list[str],
) -> None:
    state = await session.scalar(
        select(SolverState).where(SolverState.run_id == control_run_id)
    )
    if state is None:
        return
    state.active_skill_ids_json = sorted(set(active_skill_ids))
    await session.flush()


def _copy_source_configuration(
    source_run: SolveRun,
    control_run: SolveRun,
) -> None:
    for field_name in _COPIED_SCALAR_FIELDS:
        setattr(control_run, field_name, getattr(source_run, field_name))
    control_run.role_snapshot_json = dict(source_run.role_snapshot_json or {})


def _source_knowledge_mode(hints: Mapping[str, Any]) -> str:
    try:
        runtime_default = load_knowledge_config().mode
        return str(resolve_knowledge_mode(hints, runtime_default))
    except ValueError as error:
        raise DomainError(
            "SOURCE_KNOWLEDGE_MODE_INVALID",
            "The source Run has an invalid knowledge_mode.",
            {"knowledge_mode": hints.get("knowledge_mode")},
            status_code=409,
        ) from error


def _ensure_source_control_eligible(source_run: SolveRun) -> None:
    try:
        status = RunStatus(str(source_run.status))
    except ValueError as error:
        raise DomainError(
            "SOURCE_RUN_STATUS_INVALID",
            "The source Run has an invalid lifecycle status.",
            {"status": source_run.status},
            status_code=409,
        ) from error
    if status not in TERMINAL and status not in ACTIVE_EXECUTION_STATUSES:
        raise DomainError(
            "SOURCE_RUN_NOT_CONTROL_ELIGIBLE",
            "A control experiment can only be created for an actively executing or terminal Run.",
            {
                "status": status.value,
                "allowed_active_statuses": sorted(
                    item.value for item in ACTIVE_EXECUTION_STATUSES
                ),
                "allowed_terminal_statuses": sorted(
                    item.value for item in TERMINAL
                ),
            },
            status_code=409,
        )


async def _ensure_source_challenge_is_evaluatable(
    session: AsyncSession,
    source_run: SolveRun,
) -> None:
    """Refuse to pair a Run whose Challenge has no evaluator case binding.

    A/B pairing exists to produce *comparable, scorable* evidence.  A challenge
    without ``metadata_json.benchmark_case_id`` cannot be scored by the
    benchmark evaluator, so both arms would come back with
    ``evaluation_report=null`` / ``success=null`` and the pair would be unusable
    no matter how much compute it burned — which is exactly what happened to
    experiment 8ddbdb34, where the failure was only discovered in review.  This
    gate is what stops such a pair from being produced at all.

    Scope note: only the *presence* of the binding is enforced.  Whether the id
    resolves to a case file is deliberately not checked here, because benchmark
    case directories are configurable and a false refusal would be worse than a
    late one.
    """

    challenge = await session.get(Challenge, source_run.challenge_id)
    metadata = getattr(challenge, "metadata_json", None)
    case_id = ""
    if isinstance(metadata, Mapping):
        case_id = str(metadata.get("benchmark_case_id") or "").strip()
    if case_id:
        return

    raise DomainError(
        "SOURCE_CHALLENGE_NOT_EVALUATABLE",
        (
            "The source Run's challenge is not bound to a benchmark evaluator "
            "case, so a paired experiment could not be scored."
        ),
        {
            "challenge_id": str(source_run.challenge_id),
            "benchmark_case_id": None,
            "remedy": (
                "Bind metadata_json.benchmark_case_id on the challenge, or run "
                "the case through the benchmark experiment path which sets it."
            ),
        },
        status_code=409,
    )


def _pairing_lock(run_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    return _PAIRING_LOCKS.setdefault((loop, str(run_id)), asyncio.Lock())


def _hints(run: SolveRun) -> Mapping[str, Any]:
    hints = run.hints_json
    return hints if isinstance(hints, Mapping) else {}


def _persistent_hints(hints: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in hints.items()
        if key not in _TRANSIENT_HINT_KEYS
    }


def _experiment_metadata(run: SolveRun) -> Mapping[str, Any] | None:
    metadata = _hints(run).get("ab_experiment")
    return metadata if isinstance(metadata, Mapping) else None


def _created_run_id(created: Mapping[str, Any]) -> str:
    data = created.get("data") if isinstance(created, Mapping) else None
    run_id = getattr(data, "id", None)
    if run_id is None and isinstance(data, Mapping):
        run_id = data.get("id")
    if not run_id:
        raise DomainError(
            "CONTROL_RUN_CREATE_FAILED",
            "The existing Run creation flow returned no control Run id.",
            status_code=500,
        )
    return str(run_id)


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        return


__all__ = [
    "AB_EXPERIMENT_SCHEMA_VERSION",
    "BASELINE_VALUE",
    "CONTROL_FACTOR",
    "TREATMENT_VALUE",
    "create_control_experiment",
]
