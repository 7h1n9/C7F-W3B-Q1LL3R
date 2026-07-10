from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.schemas.challenge import ChallengeInput, ChallengeRead

router = APIRouter(prefix="/challenges", tags=["challenges"])


def read(item: Challenge) -> ChallengeRead:
    return ChallengeRead.model_validate({**item.__dict__, "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()})


async def require_challenge(challenge_id: str, session: AsyncSession) -> Challenge:
    item = await session.scalar(select(Challenge).where(Challenge.id == challenge_id))
    if not item:
        raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
    return item


@router.get("")
async def list_challenges(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(Challenge).order_by(Challenge.created_at.desc()))).all())
    return {"data": [read(item) for item in items]}


@router.post("", status_code=201)
async def create_challenge(payload: ChallengeInput, session: AsyncSession = Depends(get_session)) -> dict:
    item = Challenge(**payload.model_dump())
    session.add(item); await session.commit(); await session.refresh(item)
    return {"data": read(item)}


@router.get("/{challenge_id}")
async def get_challenge(challenge_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": read(await require_challenge(challenge_id, session))}


@router.put("/{challenge_id}")
async def update_challenge(challenge_id: str, payload: ChallengeInput, session: AsyncSession = Depends(get_session)) -> dict:
    item = await require_challenge(challenge_id, session)
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    await session.commit(); await session.refresh(item)
    return {"data": read(item)}


@router.delete("/{challenge_id}", status_code=204)
async def delete_challenge(challenge_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    item = await require_challenge(challenge_id, session)
    await session.delete(item); await session.commit()
    return Response(status_code=204)
