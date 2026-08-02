import asyncio
import contextlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.challenges import require_challenge
from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import Challenge, ChallengeAttachment
from app.models.conversation import (
    ChallengeConversation,
    ChallengeConversationSkill,
    ChallengeMessage,
)
from app.models.learned_skill import (
    LearnedSkillCandidate,
    LearnedSkillCandidateSource,
    LearnedSkillReview,
    LearnedSkillValidationRun,
)
from app.models.model_config import ModelConfig
from app.models.multi_agent import (
    AgentTask,
    AgentTaskResult,
    AnalysisReview,
    ApprovedAction,
    EvidenceLedger,
    FailureSignature,
    MemorySnapshot,
    PlannerProposal,
    SolutionChainNode,
    VerifiedFact,
)
from app.models.run import (
    AgentTurn,
    Artifact,
    AttemptToolManifest,
    CleanupManifest,
    CompactionLease,
    EvidenceSnapshot,
    FlagCandidate,
    FlagProvenance,
    Hypothesis,
    LogicalToolCall,
    Observation,
    RunAttempt,
    RunCompactionCheckpoint,
    RunEvent,
    RunExecutionLease,
    RunUserInput,
    ScriptRecord,
    SolveRun,
    ToolBatchSummary,
    ToolCall,
    ToolExecutionTrace,
    ToolInvocationTicket,
    ToolRequestFingerprint,
    WebResearchRecord,
)
from app.models.skill import RunSkillSnapshot
from app.models.solver_state import SolverState
from app.orchestration.orchestrator import orchestrator
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.orchestration.state_machine import restart as restart_state
from app.schemas.compaction import CompactionDecisionAction
from app.schemas.flag import FlagReviewUpdate
from app.schemas.run import RunBatchDelete, RunCreate, RunRead
from app.schemas.solver_state import SolverStateRead
from app.services.assistance import classify_user_input
from app.services.codex_materializer import codex_materializer
from app.services.codex_preflight import codex_preflight_service
from app.services.compaction import compaction_service
from app.services.events import event_service
from app.services.flags import flag_service
from app.services.fresh_reproduction import fresh_reproduction_executor
from app.services.methodology_hints import hints_for_challenge
from app.services.role_loader import role_loader
from app.services.run_attempts import run_attempt_service
from app.services.run_lifecycle import cancel_run as cancel_run_lifecycle
from app.services.run_finalizer import run_finalizer
from app.services.run_supervisor import run_supervisor
from app.services.run_diagnostics import run_diagnostics_service
from app.services.runner_client import runner_client
from app.services.skill_selection import snapshot_run_skills
from app.services.solver_state import solver_state_service
from app.services.workspace import create_workspace

router = APIRouter(tags=["runs"])
LOCAL_TARGET_HOSTS = {"localhost", "127.0.0.1", "::1"}


def target_is_local_to_backend(challenge: object) -> bool:
    target_url = getattr(challenge, "target_url", None)
    return bool(target_url and (urlparse(target_url).hostname or "").lower() in LOCAL_TARGET_HOSTS)


def runner_is_remote() -> bool:
    runner_host = (urlparse(get_settings().runner_url).hostname or "").lower()
    return runner_host not in LOCAL_TARGET_HOSTS


def remote_local_target_blocked(challenge: object, engine_type: str) -> bool:
    return (
        engine_type != "mock"
        and getattr(challenge, "challenge_type", "WEB_TARGET") == "WEB_TARGET"
        and target_is_local_to_backend(challenge)
        and runner_is_remote()
        and not get_settings().allow_remote_local_targets
    )


def read(item: SolveRun) -> RunRead:
    def iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    payload = {
        **item.__dict__,
        # Columns introduced by later migrations can still be NULL on legacy
        # rows.  Keep the API contract stable for those rows.
        "recovery_checkpoint_json": item.recovery_checkpoint_json or {},
        "role_snapshot_json": item.role_snapshot_json or {},
        "hints_json": item.hints_json or {},
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
        "started_at": iso(item.started_at),
        "finished_at": iso(item.finished_at),
    }
    return RunRead.model_validate(payload)


async def read_with_summary(
    session: AsyncSession, item: SolveRun, *, include_diagnostics: bool = True
) -> RunRead:
    challenge = await session.get(Challenge, item.challenge_id)
    model = await session.get(ModelConfig, item.model_config_id) if item.model_config_id else None
    state = await solver_state_service.load(session, item.id)
    active_skill_names: list[str] = []
    if state and state.active_skill_ids_json:
        skills = list(
            (
                await session.scalars(
                    select(RunSkillSnapshot).where(
                        RunSkillSnapshot.run_id == item.id,
                        RunSkillSnapshot.skill_id.in_(state.active_skill_ids_json),
                    )
                )
            ).all()
        )
        active_skill_names = [snapshot.skill_name for snapshot in skills]
    diagnostics = (
        await run_diagnostics_service.analyze(session, item)
        if include_diagnostics
        else {
            "diagnostic_tags": [item.last_error_code] if item.last_error_code else [],
            "diagnostic_summary": item.last_error_message,
        }
    )
    is_codex = item.engine_type == "codex_sdk"
    is_openai = item.engine_type == "openai_compatible"
    bridge_ready = bool(codex_preflight_service.last_result() and codex_preflight_service.last_result().get("ready")) if is_codex else False
    preflight_ready = codex_preflight_service.is_ready(item.id) if is_codex else False
    payload = {
        **item.__dict__,
        "recovery_checkpoint_json": item.recovery_checkpoint_json or {},
        "role_snapshot_json": item.role_snapshot_json or {},
        "hints_json": item.hints_json or {},
        "challenge_name": challenge.name if challenge else None,
        "challenge_type": challenge.challenge_type if challenge else None,
        "target_summary": challenge.target_url if challenge and challenge.target_url else None,
        "model_name": model.name if model else None,
        "model_source": "CODEX_BRIDGE" if is_codex else ("OPENAI_COMPATIBLE" if is_openai else None),
        "model_config_required": is_openai,
        "model_config_applicable": is_openai,
        "bridge_ready": bridge_ready,
        "preflight_ready": preflight_ready,
        "active_skill_names": active_skill_names,
        "diagnostic_tags": diagnostics["diagnostic_tags"],
        "diagnostic_summary": diagnostics["diagnostic_summary"],
        "created_at": read(item).created_at,
        "updated_at": read(item).updated_at,
        "started_at": read(item).started_at,
        "finished_at": read(item).finished_at,
    }
    return RunRead.model_validate(payload)


async def require_run(run_id: str, session: AsyncSession) -> SolveRun:
    item = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
    if not item:
        raise DomainError("RUN_NOT_FOUND", "Solve run not found.", status_code=404)
    return item


async def ensure_codex_materialized(session: AsyncSession, run: SolveRun) -> SolveRun:
    if run.engine_type == "codex_sdk":
        await codex_materializer.sync(session, run)
    return run


async def ensure_flag_consistency(session: AsyncSession, run: SolveRun) -> SolveRun:
    await flag_service.reconcile_run_status(session, run)
    return run


@router.post("/challenges/{challenge_id}/runs", status_code=201)
async def create_run(
    challenge_id: str, payload: RunCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    challenge = await require_challenge(challenge_id, session)
    if remote_local_target_blocked(challenge, payload.engine_type):
        raise DomainError(
            "TARGET_NOT_REACHABLE_FROM_RUNNER",
            "Remote Kali Runner cannot reach the Windows localhost target. Use the Windows LAN IP or configure a local Runner.",
            status_code=422,
        )
    if challenge.challenge_type == "TRAFFIC_ANALYSIS":
        primary = (
            await session.get(ChallengeAttachment, challenge.primary_attachment_id)
            if challenge.primary_attachment_id
            else None
        )
        if challenge.status == "DRAFT" or not primary or primary.kind != "PCAP":
            raise DomainError(
                "TRAFFIC_PCAP_REQUIRED",
                "Traffic-analysis challenges require a valid primary PCAP before creating a Run.",
                status_code=422,
            )
    values = payload.model_dump(
        exclude={"selected_skill_ids", "disabled_skill_ids", "conversation_id"}
    )
    conversation_summary = None
    if payload.conversation_id:
        conversation = await session.get(ChallengeConversation, payload.conversation_id)
        if not conversation or conversation.challenge_id != challenge.id:
            raise DomainError(
                "CONVERSATION_NOT_FOUND",
                "Conversation does not belong to this challenge.",
                status_code=422,
            )
        if values.get("model_config_id") is None:
            values["model_config_id"] = conversation.model_config_id
        if not payload.selected_skill_ids:
            payload.selected_skill_ids = [
                item.skill_id
                for item in (
                    await session.scalars(
                        select(ChallengeConversationSkill)
                        .where(ChallengeConversationSkill.conversation_id == conversation.id)
                        .order_by(ChallengeConversationSkill.priority)
                    )
                ).all()
            ]
        messages = list(
            (
                await session.scalars(
                    select(ChallengeMessage)
                    .where(ChallengeMessage.conversation_id == conversation.id)
                    .order_by(ChallengeMessage.created_at.desc())
                    .limit(8)
                )
            ).all()
        )
        conversation_summary = "\n".join(
            f"{message.role}: {message.content}" for message in reversed(messages)
        )[:8000]
    if payload.engine_type == "openai_compatible":
        from app.models.model_config import ModelConfig

        config = (
            await session.get(ModelConfig, values.get("model_config_id"))
            if values.get("model_config_id")
            else None
        )
        if not config or not config.enabled:
            raise DomainError(
                "MODEL_CONFIG_REQUIRED",
                "OpenAI-compatible runs require an enabled model configuration.",
                status_code=422,
            )
    if payload.engine_type == "codex_sdk" and values.get("model_config_id"):
        raise DomainError(
            "MODEL_CONFIG_NOT_APPLICABLE",
            "Codex SDK runs do not use a model configuration.",
            status_code=422,
        )
    item = SolveRun(
        challenge_id=challenge.id,
        workspace_path="pending",
        conversation_summary=conversation_summary,
        role_name=None,
        role_version=None,
        role_snapshot_json={},
        **values,
    )
    item.hints_json = hints_for_challenge(challenge)
    session.add(item)
    await session.flush()
    role = role_loader.load(challenge.challenge_type)
    item.role_name = role.name
    item.role_version = role.version
    item.role_snapshot_json = role.snapshot()
    attachments = list(
        (
            await session.scalars(
                select(ChallengeAttachment).where(ChallengeAttachment.challenge_id == challenge.id)
            )
        ).all()
    )
    item.workspace_path = str(create_workspace(item.id, challenge, attachments))
    snapshots = await snapshot_run_skills(
        session,
        item.id,
        challenge.id,
        challenge.challenge_type,
        item.model_config_id,
        payload.selected_skill_ids,
        payload.disabled_skill_ids,
    )
    await solver_state_service.initialize(
        session,
        item,
        challenge.challenge_type,
        [snapshot.skill_id for snapshot in snapshots],
        challenge.name,
        challenge.description,
    )
    await session.commit()
    await session.refresh(item)
    await event_service.append(session, item.id, "run.created", {"challenge_id": challenge.id})
    return {"data": read(item)}


@router.get("/runs")
async def list_runs(session: AsyncSession = Depends(get_session)) -> dict:
    items = list(
        (await session.scalars(select(SolveRun).order_by(SolveRun.created_at.desc()))).all()
    )
    payload = []
    for item in items:
        # The list view must stay lightweight.  Codex materialization and deep
        # diagnostics scan all events/tool outputs for a run; doing that for
        # every row turns ordinary page loads into repeated full-history
        # reprocessing.  Detail/diagnostic endpoints still materialize on
        # demand before returning workspace-level data.
        payload.append(await read_with_summary(session, item, include_diagnostics=False))
    return {"data": payload}


def _remove_local_workspace(workspace_path: str) -> None:
    root = get_settings().workspace_root.resolve()
    workspace = Path(workspace_path).resolve()
    if workspace != root and root in workspace.parents and workspace.name:
        shutil.rmtree(workspace, ignore_errors=True)


async def _delete_run_records(session: AsyncSession, run_id: str) -> None:
    # Delete children explicitly because the schema intentionally keeps these
    # tables unconfigured with ORM cascade rules.
    logical_ids = select(LogicalToolCall.id).where(LogicalToolCall.run_id == run_id)
    task_ids = select(AgentTask.id).where(AgentTask.run_id == run_id)
    proposal_ids = select(PlannerProposal.id).where(PlannerProposal.run_id == run_id)
    candidate_ids = select(LearnedSkillCandidate.id).where(LearnedSkillCandidate.source_run_id == run_id)
    # Tool traces reference logical calls, so they must be removed first.
    await session.execute(
        delete(ToolExecutionTrace).where(ToolExecutionTrace.logical_tool_call_id.in_(logical_ids))
    )
    # These tables reference run-scoped task/proposal/candidate rows rather
    # than carrying run_id themselves.
    await session.execute(delete(AgentTaskResult).where(AgentTaskResult.task_id.in_(task_ids)))
    # ApprovedAction points to both the review and proposal, while ToolCall
    # points back to ApprovedAction. Clear the nullable ToolCall reference
    # before removing ApprovedAction; ToolCall itself is deleted below after
    # its artifact/observation dependencies.
    await session.execute(
        update(ToolCall).where(ToolCall.run_id == run_id).values(approved_action_id=None)
    )
    await session.execute(delete(ApprovedAction).where(ApprovedAction.run_id == run_id))
    await session.execute(delete(AnalysisReview).where(AnalysisReview.proposal_id.in_(proposal_ids)))
    await session.execute(
        delete(LearnedSkillCandidateSource).where(LearnedSkillCandidateSource.candidate_id.in_(candidate_ids))
    )
    await session.execute(
        delete(LearnedSkillReview).where(LearnedSkillReview.candidate_id.in_(candidate_ids))
    )
    await session.execute(
        delete(LearnedSkillValidationRun).where(LearnedSkillValidationRun.candidate_id.in_(candidate_ids))
    )
    for model in (
        ToolInvocationTicket,
        LogicalToolCall,
        FlagProvenance,
        FlagCandidate,
        EvidenceLedger,
        SolutionChainNode,
        VerifiedFact,
        MemorySnapshot,
        ScriptRecord,
        ToolBatchSummary,
        RunUserInput,
        AttemptToolManifest,
        RunExecutionLease,
        # LogicalToolCall.result_observation_id points back to observations.
        # It must be removed before the referenced observations.
        Observation,
        Artifact,
        ToolCall,
        EvidenceSnapshot,
        RunCompactionCheckpoint,
        CompactionLease,
        CleanupManifest,
        ToolRequestFingerprint,
        WebResearchRecord,
        Hypothesis,
        RunEvent,
        AgentTurn,
        RunSkillSnapshot,
        SolverState,
        RunAttempt,
        FailureSignature,
        PlannerProposal,
    ):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.execute(delete(LearnedSkillValidationRun).where(LearnedSkillValidationRun.run_id == run_id))
    await session.execute(delete(LearnedSkillCandidate).where(LearnedSkillCandidate.source_run_id == run_id))
    # AgentTask has a nullable self-reference; clear it before deleting the
    # run's task tree so sibling/child tasks cannot block the parent delete.
    await session.execute(
        update(AgentTask).where(AgentTask.run_id == run_id).values(created_by_task_id=None)
    )
    await session.execute(delete(AgentTask).where(AgentTask.run_id == run_id))
    await session.execute(delete(SolveRun).where(SolveRun.id == run_id))


async def _delete_run(session: AsyncSession, run: SolveRun) -> None:
    task = orchestrator.active_tasks.get(run.id)
    if task and task is not asyncio.current_task():
        task.cancel()
        # A running Codex task may already have a failed transaction.  Roll
        # back after cancellation so the delete can use a clean transaction.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await session.rollback()
    with contextlib.suppress(Exception):
        await runner_client.delete_workspace(run.id)
    _remove_local_workspace(run.workspace_path)
    await _delete_run_records(session, run.id)


@router.delete("/runs/batch")
async def delete_runs(payload: RunBatchDelete, session: AsyncSession = Depends(get_session)) -> dict:
    run_ids = list(dict.fromkeys(payload.run_ids))
    runs = list((await session.scalars(select(SolveRun).where(SolveRun.id.in_(run_ids)))).all())
    found_ids = {run.id for run in runs}
    missing_ids = [run_id for run_id in run_ids if run_id not in found_ids]
    if missing_ids:
        raise DomainError(
            "RUN_NOT_FOUND",
            "One or more selected Runs do not exist.",
            {"missing_run_ids": missing_ids},
            status_code=404,
        )
    runs_by_id = {run.id: run for run in runs}
    for run_id in run_ids:
        await _delete_run(session, runs_by_id[run_id])
    await session.commit()
    return {"data": {"deleted_count": len(run_ids), "run_ids": run_ids}}


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, session: AsyncSession = Depends(get_session)) -> None:
    run = await require_run(run_id, session)
    await _delete_run(session, run)
    await session.commit()


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    # Diagnostics scan the complete event/tool history. They are useful for
    # terminal/error views, but doing that work on every live polling request
    # makes the counters visibly lag as a run grows.
    include_diagnostics = RunStatus(run.status) in TERMINAL or bool(run.last_error_code)
    return {"data": await read_with_summary(session, run, include_diagnostics=include_diagnostics)}


@router.get("/runs/{run_id}/solver-state")
async def get_solver_state(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    state = await solver_state_service.load(session, run.id)
    if not state:
        raise DomainError("SOLVER_STATE_NOT_FOUND", "Solver state not found.", status_code=404)
    payload = {
        "id": state.id,
        "run_id": state.run_id,
        "current_phase": state.current_phase,
        "confirmed_facts_json": state.confirmed_facts_json,
        "rejected_paths_json": state.rejected_paths_json,
        "active_hypotheses_json": state.active_hypotheses_json,
        "action_fingerprints_json": state.action_fingerprints_json,
        "active_skill_ids_json": state.active_skill_ids_json,
        "skill_recommendations_json": state.skill_recommendations_json or [],
        "run_plan_json": state.run_plan_json or {},
        "capability_ledger_json": state.capability_ledger_json or {},
        "read_files_json": state.read_files_json or [],
        "read_ranges_json": state.read_ranges_json or [],
        "content_hashes_json": state.content_hashes_json or {},
        "last_decision_card_json": state.last_decision_card_json or {},
        "last_experiment_json": state.last_experiment_json or {},
        "no_progress_count": state.no_progress_count,
        "last_progress_at": state.last_progress_at.replace(tzinfo=UTC).astimezone(UTC).isoformat() if state.last_progress_at and state.last_progress_at.tzinfo is None else (state.last_progress_at.astimezone(UTC).isoformat() if state.last_progress_at else None),
        "created_at": state.created_at.replace(tzinfo=UTC).astimezone(UTC).isoformat() if state.created_at.tzinfo is None else state.created_at.astimezone(UTC).isoformat(),
        "updated_at": state.updated_at.replace(tzinfo=UTC).astimezone(UTC).isoformat() if state.updated_at.tzinfo is None else state.updated_at.astimezone(UTC).isoformat(),
    }
    return {
        "data": SolverStateRead.model_validate(payload)
    }


@router.get("/runs/{run_id}/tool-manifest")
async def get_run_tool_manifest(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    item = await session.scalar(
        select(AttemptToolManifest).where(AttemptToolManifest.run_id == run.id).order_by(AttemptToolManifest.created_at.desc())
    )
    if not item:
        raise DomainError("TOOL_MANIFEST_NOT_FOUND", "No Attempt Tool Manifest has been recorded.", status_code=404)
    return {"data": {"id": item.id, "run_id": item.run_id, "attempt_id": item.attempt_id, "role_snapshot_tools": item.role_snapshot_tools, "challenge_allowed_tools": item.challenge_allowed_tools, "backend_registry_tools": item.backend_registry_tools, "runner_capability_tools": item.runner_capability_tools, "mcp_advertised_tools": item.mcp_advertised_tools, "execution_mode": item.execution_mode, "mcp_required": item.mcp_required, "effective_tools": item.effective_tools, "missing_expected_tools": item.missing_expected_tools, "schema_hashes": item.schema_hashes, "manifest_sha256": item.manifest_sha256, "created_at": item.created_at.isoformat()}}


@router.post("/runs/{run_id}/start")
async def start_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    await run_finalizer.reconcile(session, run)
    await run_attempt_service.recover_stale_execution(session, run)
    await run_attempt_service.reclaim_expired_lease(session, run.id)
    active_lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
    if active_lease:
        raise DomainError("RUN_ALREADY_EXECUTING", "Run already has an active execution lease.", status_code=409)
    checkpoint_recovery = (
        run.status == RunStatus.WAITING_USER
        and isinstance(run.recovery_checkpoint_json, dict)
        and bool(run.recovery_checkpoint_json.get("next_required_action"))
        and run.current_phase == "FLAG_SEARCH"
    )
    recoverable_planning = run.status in {RunStatus.PLANNING, RunStatus.ANALYZING, RunStatus.EVALUATING} and bool(run.last_error_code)
    if run.status not in {RunStatus.CREATED, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_DEPLOYMENT, RunStatus.PAUSED_CHECKPOINT, RunStatus.WAITING_CONFIGURATION} and not checkpoint_recovery and not recoverable_planning:
        raise DomainError(
            "RUN_INVALID_STATE",
            "Only created or controller-recoverable runs can be started.",
            {"current_state": run.status},
        )
    if recoverable_planning or run.status in {RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_CHECKPOINT}:
        run.last_error_code = None
        run.last_error_message = None
        await session.commit()
    if run.status == RunStatus.PAUSED_CHECKPOINT and run.started_at:
        started_at = run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at
        if (datetime.now(UTC) - started_at).total_seconds() >= run.max_total_runtime_seconds:
            raise DomainError(
                "MAX_TOTAL_RUNTIME",
                "任务累计运行时间已达到上限，请点击“重启任务”开启新的运行窗口。",
                {"max_total_runtime_seconds": run.max_total_runtime_seconds},
                status_code=409,
            )
    asyncio.create_task(run_supervisor.run_background(run.id))
    return {"data": {"run_id": run.id, "status": "STARTING"}}


@router.get("/runs/{run_id}/messages")
async def list_run_messages(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_run(run_id, session)
    items = list(
        (await session.scalars(select(RunUserInput).where(RunUserInput.run_id == run_id).order_by(RunUserInput.revision))).all()
    )
    return {"data": [{"id": item.id, "content": item.content, "input_type": item.input_type, "status": item.status, "revision": item.revision, "created_at": item.created_at.isoformat(), "consumed_at": item.consumed_at.isoformat() if item.consumed_at else None} for item in items]}


@router.get("/runs/{run_id}/health")
async def run_health(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Expose the durable reasons a Run is waiting or not progressing."""
    run = await require_run(run_id, session)
    lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
    running_task = await session.scalar(select(AgentTask.id).where(
        AgentTask.run_id == run.id,
        AgentTask.status.in_(["RUNNING", "CLAIMED"]),
    ))
    running_tool = await session.scalar(select(ToolCall.id).where(
        ToolCall.run_id == run.id,
        ToolCall.status.in_(["REQUESTED", "STARTED", "RUNNING"]),
    ))
    queued_inputs = int(await session.scalar(select(func.count()).select_from(RunUserInput).where(
        RunUserInput.run_id == run.id,
        RunUserInput.status == "QUEUED",
        RunUserInput.consumed_at.is_(None),
    )) or 0)
    consumed_inputs = int(await session.scalar(select(func.count()).select_from(RunUserInput).where(
        RunUserInput.run_id == run.id,
        RunUserInput.status == "CONSUMED",
    )) or 0)
    last_fact = await session.scalar(select(VerifiedFact).where(
        VerifiedFact.run_id == run.id,
    ).order_by(VerifiedFact.updated_at.desc(), VerifiedFact.created_at.desc()))
    last_tool = await session.scalar(select(ToolCall).where(
        ToolCall.run_id == run.id,
    ).order_by(ToolCall.created_at.desc()))
    checkpoint = dict(run.recovery_checkpoint_json or {})
    counters = dict(checkpoint.get("supervisor_counters") or {})
    status = str(run.status)
    if queued_inputs:
        next_action = "consume_user_input"
    elif status == RunStatus.WAITING_USER.value:
        next_action = "wait_for_user_input"
    elif status in {item.value for item in TERMINAL}:
        next_action = "terminal"
    elif lease is not None or running_task or running_tool:
        next_action = "execute_current_attempt"
    elif int(counters.get("no_progress_count") or 0) > 0:
        next_action = "finish_unsolved_with_wp"
    else:
        next_action = "continue_supervisor"
    return {"data": {
        "status": status,
        "current_phase": run.current_phase,
        "last_error_code": run.last_error_code,
        "runtime": {
            "active_lease": lease is not None,
            "lease_owner": lease.owner_instance_id if lease else None,
            "running_task": running_task is not None,
            "running_tool": running_tool is not None,
        },
        "input": {"queued": queued_inputs, "consumed": consumed_inputs},
        "progress": {
            "last_fact": last_fact.fact_key if last_fact else None,
            "last_tool": last_tool.tool_name if last_tool else None,
            "last_tool_status": last_tool.status if last_tool else None,
            "no_progress_count": int(counters.get("no_progress_count") or 0),
        },
        "next_action": next_action,
        "checkpoint": checkpoint,
    }}


@router.post("/runs/{run_id}/messages")
async def enqueue_run_message(run_id: str, payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    if RunStatus(run.status) in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
        raise DomainError("RUN_TERMINAL", "Cannot add information to a terminal run.", status_code=409)
    content = str(payload.get("content") or "").strip()
    if not content:
        raise DomainError("MESSAGE_EMPTY", "Supplemental information cannot be empty.", status_code=422)
    latest = await session.scalar(select(RunUserInput.revision).where(RunUserInput.run_id == run.id).order_by(RunUserInput.revision.desc()))
    revision = int(latest or 0) + 1
    item = RunUserInput(run_id=run.id, content=content[:16000], input_type=str(payload.get("input_type") or "SUPPLEMENT")[:40], status="QUEUED", revision=revision)
    session.add(item)
    guidance_level = classify_user_input(content)
    level_rank = {"AUTONOMOUS": 0, "HINT_GUIDED": 1, "EVIDENCE_GUIDED": 2, "ANSWER_GUIDED": 3}
    if level_rank.get(guidance_level, 0) > level_rank.get(run.assistance_level or "AUTONOMOUS", 0):
        run.assistance_level = guidance_level
    sources = list(run.assistance_sources_json or [])
    sources.append({"type": guidance_level, "revision": revision, "source": "USER_INPUT"})
    run.assistance_sources_json = sources[-100:]
    run.context_revision += 1
    await session.commit()
    await event_service.append(session, run.id, "user_input.received", {"revision": revision, "input_type": item.input_type})
    # Persisting the input is not enough: the durable event is followed by an
    # explicit Supervisor wakeup.  The Supervisor owns deduplication and
    # continues only after it has consumed the queued rows.
    await run_supervisor.enqueue(run.id, reason="USER_INPUT_RECEIVED")
    return {"data": {"accepted": True, "revision": revision, "status": "QUEUED", "message": "补充信息已加入，将在下一 Agent Step 使用。"}}


@router.post("/runs/{run_id}/restart")
async def restart_run(
    run_id: str, payload: dict | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    """Restart a run while retaining its workspace, evidence, events and solver state."""
    run = await require_run(run_id, session)
    await run_finalizer.reconcile(session, run)
    active = orchestrator.active_tasks.get(run_id)
    if active and not active.done():
        raise DomainError(
            "RUN_ALREADY_ACTIVE", "The run is already executing.", status_code=409
        )
    await run_attempt_service.reclaim_expired_lease(session, run.id)
    await run_attempt_service.recover_stale_execution(session, run)
    active_lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
    if active_lease:
        raise DomainError("RUN_ALREADY_EXECUTING", "Run already has an active execution lease.", status_code=409)
    restart_mode = str((payload or {}).get("mode", "resume")).lower()
    if restart_mode not in {"resume", "fresh"}:
        raise DomainError("RESTART_MODE_INVALID", "Restart mode must be resume or fresh.", status_code=422)
    previous_status = run.status
    previous_error = {
        "code": run.last_error_code,
        "message": run.last_error_message,
    }
    runtime_window_reset = restart_mode == "fresh"
    if run.started_at and previous_status in {RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_DEPLOYMENT}:
        started_at = run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at
        runtime_window_reset = runtime_window_reset or (
            datetime.now(UTC) - started_at
        ).total_seconds() >= run.max_total_runtime_seconds
    # The MCP subprocess captures the lease in its process environment when
    # the Codex thread is created. A restart creates a new Attempt/Lease, so
    # the old Bridge thread is intentionally not reusable.
    run.codex_thread_id = None
    challenge = await session.get(Challenge, run.challenge_id)
    if not challenge:
        raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
    attachments = list(
        (
            await session.scalars(
                select(ChallengeAttachment).where(
                    ChallengeAttachment.challenge_id == challenge.id
                )
            )
        ).all()
    )
    # A preserved Run workspace may contain an older target/allowlist after the
    # challenge was edited. Refresh only generated run metadata; evidence and
    # agent-created files remain intact.
    run.workspace_path = str(create_workspace(run.id, challenge, attachments))
    restart_state(run)
    if runtime_window_reset:
        run.started_at = datetime.now(UTC)
    run.last_error_code = None
    run.last_error_message = None
    await session.commit()
    if restart_mode == "fresh":
        state = await solver_state_service.load(session, run.id)
        if state:
            state.no_progress_count = 0
            state.finish_rejection_count = 0
            state.force_plan_action = 0
            state.last_decision_card_json = {}
            state.last_experiment_json = {}
            state.experiment_dimensions_json = []
            await session.commit()
        with contextlib.suppress(Exception):
            await runner_client.clear_sessions(run.id)
    await solver_state_service.sync_from_run(session, run)
    await event_service.append(
        session,
        run.id,
        "run.restarted",
        {
            "previous_status": previous_status,
            "previous_error": previous_error,
            "preserved_workspace": True,
            "preserved_evidence": True,
            "restart_mode": restart_mode,
            "codex_thread_id_reused": False,
            "runtime_window_reset": runtime_window_reset,
        },
    )
    message = str((payload or {}).get("message", "")).strip() or None
    asyncio.create_task(run_supervisor.run_background(run.id, message))
    return {"data": {"run_id": run.id, "status": "STARTING"}}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    await cancel_run_lifecycle(session, run.id, "Run cancelled by user.")
    await run_finalizer.reconcile(session, run)
    await orchestrator.cancel(run.id)
    with contextlib.suppress(Exception):
        await runner_client.clear_sessions(run.id)
    return {"data": read(run)}


@router.post("/runs/{run_id}/continue")
async def continue_run(
    run_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    run = await require_run(run_id, session)
    await run_finalizer.reconcile(session, run)
    await run_attempt_service.reclaim_expired_lease(session, run.id)
    active_lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
    if active_lease:
        raise DomainError("RUN_ALREADY_EXECUTING", "Run already has an active execution lease.", status_code=409)
    if run.status not in {RunStatus.WAITING_USER, RunStatus.PAUSED_RATE_LIMIT}:
        raise DomainError(
            "RUN_NOT_WAITING", "Only runs waiting for user input can continue.", status_code=409
        )
    message = str(payload.get("message", "")).strip()
    if not message:
        raise DomainError(
            "MESSAGE_REQUIRED", "A continuation message is required.", status_code=422
        )
    asyncio.create_task(run_supervisor.run_background(run.id, message))
    return {"data": {"run_id": run.id, "status": "STARTING"}}


@router.get("/runs/{run_id}/tool-calls")
async def list_tool_calls(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    await ensure_flag_consistency(session, run)
    items = list(
        (
            await session.scalars(
                select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)
            )
        ).all()
    )
    return {
        "data": [
            {
                "id": item.id,
                "tool_name": item.tool_name,
                "arguments": item.arguments_json,
                "status": item.status,
                "runner_job_id": item.runner_job_id,
                "logical_tool_call_id": item.logical_tool_call_id,
                "parent_tool_call_id": item.parent_tool_call_id,
                "execution_layer": item.execution_layer,
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ]
    }


@router.get("/runs/{run_id}/observations")
async def list_observations(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    await ensure_flag_consistency(session, run)
    items = list(
        (
            await session.scalars(
                select(Observation)
                .where(Observation.run_id == run_id)
                .order_by(Observation.created_at)
            )
        ).all()
    )
    return {
        "data": [
            {
                "id": item.id,
                "summary": item.summary,
                "facts": item.facts_json,
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ]
    }


@router.get("/runs/{run_id}/artifacts")
async def list_artifacts(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    await ensure_flag_consistency(session, run)
    items = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
            )
        ).all()
    )
    return {
        "data": [
            {
                "id": item.id,
                "path": item.file_path,
                "type": item.artifact_type,
                "size": item.size,
                "sha256": item.sha256,
                "summary": item.summary,
            }
            for item in items
        ]
    }


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
async def get_artifact(
    run_id: str, artifact_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    item = await session.get(Artifact, artifact_id)
    if item is None or item.run_id != run.id:
        raise DomainError("ARTIFACT_NOT_FOUND", "Artifact not found.", status_code=404)
    root = Path(run.workspace_path).resolve()
    path = (root / item.file_path).resolve()
    if root not in path.parents or not path.is_file():
        raise DomainError("ARTIFACT_PATH_INVALID", "Artifact file is unavailable.", status_code=404)
    return {
        "data": {
            "id": item.id,
            "path": item.file_path,
            "content": path.read_bytes()[:1_048_576].decode(errors="replace"),
            "truncated": path.stat().st_size > 1_048_576,
        }
    }


@router.get("/runs/{run_id}/flag-candidates")
async def list_flags(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    await ensure_flag_consistency(session, run)
    challenge = await session.get(Challenge, run.challenge_id)
    items = list(
        (await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run_id))).all()
    )
    if challenge:
        items = [
            item
            for item in items
            if flag_service._is_displayable(item.candidate, challenge.flag_pattern)
        ]
    # Older runs could persist the same candidate more than once when a tool
    # result was materialized repeatedly. Keep one row per candidate, preferring
    # a manually validated review state.
    unique_items: dict[str, FlagCandidate] = {}
    for item in items:
        previous = unique_items.get(item.candidate)
        if previous is None or (item.review_state == "VALID" and previous.review_state != "VALID"):
            unique_items[item.candidate] = item
    items = list(unique_items.values())
    return {
        "data": [
            {
                "id": item.id,
                "candidate": item.candidate,
                "verified": item.verified,
                "review_state": item.review_state,
                "pattern_matched": item.pattern_matched,
            }
            for item in items
        ]
    }


@router.patch("/runs/{run_id}/flag-candidates/{candidate_id}")
async def review_flag_candidate(
    run_id: str,
    candidate_id: str,
    payload: FlagReviewUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    await ensure_flag_consistency(session, run)
    try:
        item = await flag_service.set_review_state(session, run, candidate_id, payload.review_state)
    except ValueError as error:
        raise DomainError("FLAG_CANDIDATE_NOT_FOUND", str(error), status_code=404) from error
    return {
        "data": {
            "id": item.id,
            "candidate": item.candidate,
            "verified": item.verified,
            "review_state": item.review_state,
            "pattern_matched": item.pattern_matched,
        }
    }


@router.post("/runs/{run_id}/fresh-reproduction")
async def fresh_reproduction(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    challenge = await session.get(Challenge, run.challenge_id)
    if not challenge:
        raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
    from app.services.reports import ReproductionPlanner

    steps = await ReproductionPlanner().plan(session, run, challenge)
    validation = await fresh_reproduction_executor.execute(session, run, challenge, steps)
    return {"data": validation, "fresh_reproduction_verified": bool(run.fresh_reproduction_verified)}


@router.post("/runs/{run_id}/recover-solved")
async def recover_solved(run_id: str, payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Finalize a run from durable, already-verified Codex evidence after SDK transport recovery."""
    run = await require_run(run_id, session)
    challenge = await session.get(Challenge, run.challenge_id)
    if not challenge:
        raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
    candidate = str(payload.get("candidate") or "").strip()
    if not candidate:
        raise DomainError("FLAG_CANDIDATE_REQUIRED", "A flag candidate is required.", status_code=422)
    if not flag_service._is_displayable(candidate, challenge.flag_pattern):
        raise DomainError("FLAG_CANDIDATE_INVALID", "Candidate does not match the challenge flag pattern.", status_code=422)
    await flag_service.verify(session, run, challenge, candidate)
    validation = await fresh_reproduction_executor.execute(session, run, challenge, [])
    # A Codex SDK transport interruption can leave a gateway row in STARTED
    # even though the durable evidence has since been verified.  Once this
    # recovery path has established the terminal flag, no such invocation can
    # still be authoritative; close it so the report barrier and terminal
    # readers do not remain blocked forever.
    now = datetime.now(UTC)
    dangling_calls = list(
        (
            await session.scalars(
                select(ToolCall).where(
                    ToolCall.run_id == run.id,
                    ToolCall.status.in_(["REQUESTED", "STARTED"]),
                )
            )
        ).all()
    )
    for call in dangling_calls:
        call.status = "FAILED"
        call.finished_at = now
    dangling_logical_calls = list(
        (
            await session.scalars(
                select(LogicalToolCall).where(
                    LogicalToolCall.run_id == run.id,
                    LogicalToolCall.status.in_(["REQUESTED", "STARTED"]),
                )
            )
        ).all()
    )
    for call in dangling_logical_calls:
        call.status = "FAILED"
        call.finished_at = now
    run.last_error_code = None
    run.last_error_message = None
    await session.commit()
    return {"data": validation, "fresh_reproduction_verified": bool(run.fresh_reproduction_verified), "status": run.status}


@router.get("/runs/{run_id}/report")
async def get_report(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    if RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED}:
        raise DomainError("REPORT_NOT_AVAILABLE", "Formal writeups are only available for completed runs.", status_code=409)
    active_report = await session.scalar(select(Artifact.id).where(Artifact.run_id == run.id, Artifact.artifact_type == "report", Artifact.status == "ACTIVE"))
    if active_report is None:
        raise DomainError("REPORT_NOT_FOUND", "No active formal report has been generated for this run.", status_code=404)
    path = Path(run.workspace_path) / "final" / "writeup.md"
    if not path.is_file():
        raise DomainError(
            "REPORT_NOT_FOUND", "No report has been generated for this run.", status_code=404
        )
    report_json_path = Path(run.workspace_path) / "final" / "report.json"
    report_json = run.report_json or (json.loads(report_json_path.read_text(encoding="utf-8")) if report_json_path.is_file() else None)
    return {"data": {"content": path.read_text(encoding="utf-8"), "path": "final/writeup.md", "report_json": report_json}}


@router.get("/runs/{run_id}/compaction/status")
async def compaction_status(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    triggered, metrics = await compaction_service.should_compact(session, run)
    return {"data": {"triggered": triggered, "metrics": metrics, "status": run.compaction_status, "generation": run.compaction_generation, "snapshot_id": run.last_compaction_snapshot_id}}


@router.post("/runs/{run_id}/compaction")
async def apply_compaction(run_id: str, payload: CompactionDecisionAction, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    return {"data": await compaction_service.apply(session, run, payload)}


@router.get("/runs/{run_id}/diagnostics")
async def get_run_diagnostics(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    return {"data": await run_diagnostics_service.analyze(session, run)}


@router.get("/diagnostics/runs")
async def list_run_diagnostics(limit: int = 25, session: AsyncSession = Depends(get_session)) -> dict:
    limit = max(1, min(limit, 100))
    return {"data": await run_diagnostics_service.recent(session, limit=limit)}
