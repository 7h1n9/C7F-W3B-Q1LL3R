import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.challenges import require_challenge
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.run import Artifact, FlagCandidate, Observation, SolveRun, ToolCall
from app.orchestration.orchestrator import orchestrator
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.run import RunCreate, RunRead
from app.services.events import event_service
from app.services.workspace import create_workspace

router = APIRouter(tags=["runs"])


def read(item: SolveRun) -> RunRead:
    return RunRead.model_validate({**item.__dict__, "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat(), "started_at": item.started_at.isoformat() if item.started_at else None, "finished_at": item.finished_at.isoformat() if item.finished_at else None})


async def require_run(run_id: str, session: AsyncSession) -> SolveRun:
    item = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
    if not item:
        raise DomainError("RUN_NOT_FOUND", "Solve run not found.", status_code=404)
    return item


@router.post("/challenges/{challenge_id}/runs", status_code=201)
async def create_run(challenge_id: str, payload: RunCreate, session: AsyncSession = Depends(get_session)) -> dict:
    challenge = await require_challenge(challenge_id, session)
    if payload.engine_type == "openai_compatible":
        from app.models.model_config import ModelConfig
        config = await session.get(ModelConfig, payload.model_config_id) if payload.model_config_id else None
        if not config or not config.enabled:
            raise DomainError("MODEL_CONFIG_REQUIRED", "OpenAI-compatible runs require an enabled model configuration.", status_code=422)
    if payload.engine_type == "codex_sdk" and payload.model_config_id:
        raise DomainError("MODEL_CONFIG_NOT_APPLICABLE", "Codex SDK runs do not use a model configuration.", status_code=422)
    item = SolveRun(challenge_id=challenge.id, workspace_path="pending", **payload.model_dump())
    session.add(item); await session.flush()
    item.workspace_path = str(create_workspace(item.id, challenge))
    await session.commit(); await session.refresh(item)
    await event_service.append(session, item.id, "run.created", {"challenge_id": challenge.id})
    return {"data": read(item)}


@router.get("/runs")
async def list_runs(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(SolveRun).order_by(SolveRun.created_at.desc()))).all())
    return {"data": [read(item) for item in items]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": read(await require_run(run_id, session))}


@router.post("/runs/{run_id}/start")
async def start_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    if run.status != RunStatus.CREATED:
        raise DomainError("RUN_INVALID_STATE", "Only newly created runs can be started.", {"current_state": run.status})
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
async def continue_run(run_id: str, payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    if run.status != RunStatus.WAITING_USER:
        raise DomainError("RUN_NOT_WAITING", "Only runs waiting for user input can continue.", status_code=409)
    message = str(payload.get("message", "")).strip()
    if not message:
        raise DomainError("MESSAGE_REQUIRED", "A continuation message is required.", status_code=422)
    asyncio.create_task(orchestrator.continue_with_message(run.id, message))
    return {"data": {"run_id": run.id, "status": "STARTING"}}


@router.get("/runs/{run_id}/tool-calls")
async def list_tool_calls(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_run(run_id, session)
    items = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at))).all())
    return {"data": [{"id": item.id, "tool_name": item.tool_name, "arguments": item.arguments_json, "status": item.status, "runner_job_id": item.runner_job_id, "created_at": item.created_at.isoformat()} for item in items]}


@router.get("/runs/{run_id}/observations")
async def list_observations(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_run(run_id, session)
    items = list((await session.scalars(select(Observation).where(Observation.run_id == run_id).order_by(Observation.created_at))).all())
    return {"data": [{"id": item.id, "summary": item.summary, "facts": item.facts_json, "created_at": item.created_at.isoformat()} for item in items]}


@router.get("/runs/{run_id}/artifacts")
async def list_artifacts(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_run(run_id, session)
    items = list((await session.scalars(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at))).all())
    return {"data": [{"id": item.id, "path": item.file_path, "type": item.artifact_type, "size": item.size, "sha256": item.sha256, "summary": item.summary} for item in items]}


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
async def get_artifact(run_id: str, artifact_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    item = await session.get(Artifact, artifact_id)
    if item is None or item.run_id != run.id:
        raise DomainError("ARTIFACT_NOT_FOUND", "Artifact not found.", status_code=404)
    root = Path(run.workspace_path).resolve()
    path = (root / item.file_path).resolve()
    if root not in path.parents or not path.is_file():
        raise DomainError("ARTIFACT_PATH_INVALID", "Artifact file is unavailable.", status_code=404)
    return {"data": {"id": item.id, "path": item.file_path, "content": path.read_bytes()[:1_048_576].decode(errors="replace"), "truncated": path.stat().st_size > 1_048_576}}


@router.get("/runs/{run_id}/flag-candidates")
async def list_flags(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_run(run_id, session)
    items = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run_id))).all())
    return {"data": [{"id": item.id, "candidate": item.candidate, "verified": item.verified, "pattern_matched": item.pattern_matched} for item in items]}


@router.get("/runs/{run_id}/report")
async def get_report(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    path = Path(run.workspace_path) / "final" / "writeup.md"
    if not path.is_file():
        raise DomainError("REPORT_NOT_FOUND", "No report has been generated for this run.", status_code=404)
    return {"data": {"content": path.read_text(encoding="utf-8"), "path": "final/writeup.md"}}
