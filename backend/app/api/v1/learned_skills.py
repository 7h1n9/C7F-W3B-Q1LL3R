from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.learned_skill import LearnedSkillCandidate
from app.services.learned_skills import learned_skill_service

router = APIRouter(prefix="/learned-skill-candidates", tags=["skill-learning"])


def read(item: LearnedSkillCandidate) -> dict:
    return {"id": item.id, "name": item.name, "display_name": item.display_name, "description": item.description, "status": item.status, "source_run_id": item.source_run_id, "source_artifact_ids": item.source_artifact_ids, "source_observation_ids": item.source_observation_ids, "metadata": item.metadata_json, "security_scan": item.security_scan_json, "generalization_score": item.generalization_score, "content_markdown": item.sanitized_content, "created_at": item.created_at.isoformat()}


@router.get("")
async def list_candidates(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(LearnedSkillCandidate).order_by(LearnedSkillCandidate.created_at.desc()))).all())
    return {"data": [read(item) for item in items]}


@router.post("/from-run/{run_id}", status_code=201)
async def create_from_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": read(await learned_skill_service.create_from_run(session, run_id))}


@router.post("/{candidate_id}/review")
async def review_candidate(candidate_id: str, payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": read(await learned_skill_service.review(session, candidate_id, str(payload.get("decision") or ""), payload.get("review"), str(payload.get("reviewer") or "human")))}
