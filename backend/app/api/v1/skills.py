import hashlib

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.challenges import require_challenge
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.model_config import ModelConfig
from app.models.skill import ChallengeSkillBinding, ModelSkillBinding, Skill
from app.schemas.skill import ChallengeSkillBindingWrite, SkillBindingWrite, SkillWrite

router = APIRouter(tags=["skills"])


def read(skill: Skill, binding_count: int = 0) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "display_name": skill.display_name,
        "description": skill.description,
        "source_type": skill.source_type,
        "challenge_types": skill.challenge_types,
        "content_markdown": skill.content_markdown,
        "allowed_tools": skill.allowed_tools,
        "risk_level": skill.risk_level,
        "version": skill.version,
        "enabled": skill.enabled,
        "builtin_path": skill.builtin_path,
        "binding_count": binding_count,
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }


async def require_skill(skill_id: str, session: AsyncSession) -> Skill:
    item = await session.get(Skill, skill_id)
    if not item:
        raise DomainError("SKILL_NOT_FOUND", "Skill not found.", status_code=404)
    return item


@router.get("/skills")
async def list_skills(session: AsyncSession = Depends(get_session)) -> dict:
    counts = dict(
        (
            await session.execute(
                select(ModelSkillBinding.skill_id, func.count()).group_by(
                    ModelSkillBinding.skill_id
                )
            )
        ).all()
    )
    items = list(
        (await session.scalars(select(Skill).order_by(Skill.source_type, Skill.name))).all()
    )
    return {"data": [read(item, counts.get(item.id, 0)) for item in items]}


@router.post("/skills", status_code=201)
async def create_skill(payload: SkillWrite, session: AsyncSession = Depends(get_session)) -> dict:
    if await session.scalar(select(Skill.id).where(Skill.name == payload.name)):
        raise DomainError("SKILL_NAME_EXISTS", "Skill name already exists.", status_code=409)
    item = Skill(
        **payload.model_dump(),
        source_type="CUSTOM",
        checksum=hashlib.sha256(payload.content_markdown.encode()).hexdigest(),
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return {"data": read(item)}


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": read(await require_skill(skill_id, session))}


@router.put("/skills/{skill_id}")
async def update_skill(
    skill_id: str, payload: SkillWrite, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await require_skill(skill_id, session)
    if item.source_type == "BUILTIN":
        raise DomainError(
            "BUILTIN_SKILL_READ_ONLY",
            "Built-in Skills must be duplicated before editing.",
            status_code=409,
        )
    existing = await session.scalar(
        select(Skill).where(Skill.name == payload.name, Skill.id != item.id)
    )
    if existing:
        raise DomainError("SKILL_NAME_EXISTS", "Skill name already exists.", status_code=409)
    changed = item.content_markdown != payload.content_markdown
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    if changed:
        item.version += 1
        item.checksum = hashlib.sha256(item.content_markdown.encode()).hexdigest()
    await session.commit()
    await session.refresh(item)
    return {"data": read(item)}


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, session: AsyncSession = Depends(get_session)) -> None:
    item = await require_skill(skill_id, session)
    if item.source_type == "BUILTIN":
        raise DomainError(
            "BUILTIN_SKILL_READ_ONLY", "Built-in Skills cannot be deleted.", status_code=409
        )
    await session.delete(item)
    await session.commit()


@router.post("/skills/{skill_id}/validate")
async def validate_skill(skill_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    item = await require_skill(skill_id, session)
    SkillWrite(**{key: getattr(item, key) for key in SkillWrite.model_fields})
    return {"data": {"ok": True, "message": "Skill validation passed."}}


@router.post("/skills/{skill_id}/duplicate", status_code=201)
async def duplicate_skill(skill_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    item = await require_skill(skill_id, session)
    base, index = f"{item.name}-copy", 2
    name = base
    while await session.scalar(select(Skill.id).where(Skill.name == name)):
        name = f"{base}-{index}"
        index += 1
    copy = Skill(
        name=name,
        display_name=f"{item.display_name} 副本",
        description=item.description,
        source_type="CUSTOM",
        challenge_types=item.challenge_types,
        content_markdown=item.content_markdown,
        allowed_tools=item.allowed_tools,
        risk_level=item.risk_level,
        enabled=item.enabled,
        checksum=hashlib.sha256(item.content_markdown.encode()).hexdigest(),
    )
    session.add(copy)
    await session.commit()
    await session.refresh(copy)
    return {"data": read(copy)}


async def _replace_bindings(
    session: AsyncSession,
    model,
    owner_field: str,
    owner_id: str,
    values: list[SkillBindingWrite | ChallengeSkillBindingWrite],
) -> list[dict]:
    seen: set[str] = set()
    for value in values:
        if value.skill_id in seen:
            raise DomainError(
                "SKILL_BINDING_DUPLICATE", "A Skill may only be bound once.", status_code=422
            )
        seen.add(value.skill_id)
        if not await session.get(Skill, value.skill_id):
            raise DomainError("SKILL_NOT_FOUND", "Skill not found.", status_code=422)
    await session.execute(model.__table__.delete().where(getattr(model, owner_field) == owner_id))
    for value in values:
        data = value.model_dump()
        data[owner_field] = owner_id
        session.add(model(**data))
    await session.commit()
    return await _read_bindings(session, model, owner_field, owner_id)


async def _read_bindings(
    session: AsyncSession, model, owner_field: str, owner_id: str
) -> list[dict]:
    rows = list(
        (
            await session.scalars(
                select(model)
                .where(getattr(model, owner_field) == owner_id)
                .order_by(model.priority)
            )
        ).all()
    )
    data = []
    for row in rows:
        skill = await session.get(Skill, row.skill_id)
        if skill:
            data.append(
                {
                    "skill": read(skill),
                    "enabled": getattr(row, "enabled", True),
                    "priority": row.priority,
                    "config_json": getattr(row, "config_json", {}),
                }
            )
    return data


@router.get("/model-configs/{config_id}/skills")
async def get_model_skills(config_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    if not await session.get(ModelConfig, config_id):
        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    return {"data": await _read_bindings(session, ModelSkillBinding, "model_config_id", config_id)}


@router.put("/model-configs/{config_id}/skills")
async def set_model_skills(
    config_id: str, payload: list[SkillBindingWrite], session: AsyncSession = Depends(get_session)
) -> dict:
    if not await session.get(ModelConfig, config_id):
        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    return {
        "data": await _replace_bindings(
            session, ModelSkillBinding, "model_config_id", config_id, payload
        )
    }


@router.get("/challenges/{challenge_id}/skills")
async def get_challenge_skills(
    challenge_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    await require_challenge(challenge_id, session)
    return {
        "data": await _read_bindings(session, ChallengeSkillBinding, "challenge_id", challenge_id)
    }


@router.put("/challenges/{challenge_id}/skills")
async def set_challenge_skills(
    challenge_id: str,
    payload: list[ChallengeSkillBindingWrite],
    session: AsyncSession = Depends(get_session),
) -> dict:
    await require_challenge(challenge_id, session)
    return {
        "data": await _replace_bindings(
            session, ChallengeSkillBinding, "challenge_id", challenge_id, payload
        )
    }
