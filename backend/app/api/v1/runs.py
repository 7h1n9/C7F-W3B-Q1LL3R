import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.challenges import require_challenge
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.run import SolveRun
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
    item = SolveRun(challenge_id=challenge.id, engine_type=payload.engine_type, model_config_id=payload.model_config_id, workspace_path="pending")
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
