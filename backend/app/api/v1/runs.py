import asyncio
import contextlib
import shutil
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.challenges import require_challenge
from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import ChallengeAttachment
from app.models.conversation import (
    ChallengeConversation,
    ChallengeConversationSkill,
    ChallengeMessage,
)
from app.models.run import (
    Artifact,
    FlagCandidate,
    Hypothesis,
    Observation,
    RunEvent,
    SolveRun,
    ToolCall,
)
from app.models.skill import RunSkillSnapshot
from app.models.solver_state import SolverState
from app.orchestration.orchestrator import orchestrator
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.flag import FlagReviewUpdate
from app.schemas.run import RunCreate, RunRead
from app.schemas.solver_state import SolverStateRead
from app.services.codex_materializer import codex_materializer
from app.services.events import event_service
from app.services.flags import flag_service
from app.services.role_loader import role_loader
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


def read(item: SolveRun) -> RunRead:
    return RunRead.model_validate(
        {
            **item.__dict__,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        }
    )


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
    if (
        payload.engine_type != "mock"
        and challenge.challenge_type == "WEB_TARGET"
        and target_is_local_to_backend(challenge)
        and runner_is_remote()
    ):
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
    for item in items:
        await ensure_codex_materialized(session, item)
        await ensure_flag_consistency(session, item)
    return {"data": [read(item) for item in items]}


def _remove_local_workspace(workspace_path: str) -> None:
    root = get_settings().workspace_root.resolve()
    workspace = Path(workspace_path).resolve()
    if workspace != root and root in workspace.parents and workspace.name:
        shutil.rmtree(workspace, ignore_errors=True)


async def _delete_run_records(session: AsyncSession, run_id: str) -> None:
    # Delete children explicitly because the schema intentionally keeps these
    # tables unconfigured with ORM cascade rules.
    for model in (
        FlagCandidate,
        Observation,
        Artifact,
        ToolCall,
        Hypothesis,
        RunEvent,
        RunSkillSnapshot,
        SolverState,
    ):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.execute(delete(SolveRun).where(SolveRun.id == run_id))


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, session: AsyncSession = Depends(get_session)) -> None:
    run = await require_run(run_id, session)
    task = orchestrator.active_tasks.get(run_id)
    if task and task is not asyncio.current_task():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    with contextlib.suppress(Exception):
        await runner_client.delete_workspace(run_id)
    _remove_local_workspace(run.workspace_path)
    await _delete_run_records(session, run_id)
    await session.commit()


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    return {"data": read(run)}


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
        "no_progress_count": state.no_progress_count,
        "last_progress_at": state.last_progress_at.isoformat() if state.last_progress_at else None,
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }
    return {
        "data": SolverStateRead.model_validate(payload)
    }


@router.post("/runs/{run_id}/start")
async def start_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    if run.status != RunStatus.CREATED:
        raise DomainError(
            "RUN_INVALID_STATE",
            "Only newly created runs can be started.",
            {"current_state": run.status},
        )
    asyncio.create_task(orchestrator.start(run.id))
    return {"data": {"run_id": run.id, "status": "STARTING"}}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    transition(run, RunStatus.CANCELLED)
    await session.commit()
    await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
    await orchestrator.cancel(run.id)
    return {"data": read(run)}


@router.post("/runs/{run_id}/continue")
async def continue_run(
    run_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    run = await require_run(run_id, session)
    if run.status != RunStatus.WAITING_USER:
        raise DomainError(
            "RUN_NOT_WAITING", "Only runs waiting for user input can continue.", status_code=409
        )
    message = str(payload.get("message", "")).strip()
    if not message:
        raise DomainError(
            "MESSAGE_REQUIRED", "A continuation message is required.", status_code=422
        )
    asyncio.create_task(orchestrator.continue_with_message(run.id, message))
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
    items = list(
        (await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run_id))).all()
    )
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


@router.get("/runs/{run_id}/report")
async def get_report(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await ensure_codex_materialized(session, await require_run(run_id, session))
    run = await ensure_flag_consistency(session, run)
    path = Path(run.workspace_path) / "final" / "writeup.md"
    if not path.is_file():
        raise DomainError(
            "REPORT_NOT_FOUND", "No report has been generated for this run.", status_code=404
        )
    return {"data": {"content": path.read_text(encoding="utf-8"), "path": "final/writeup.md"}}
