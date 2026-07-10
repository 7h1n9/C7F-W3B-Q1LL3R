from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.model_config import ModelConfig

router = APIRouter(prefix="/model-configs", tags=["settings"])


@router.get("")
async def list_model_configs(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(ModelConfig).order_by(ModelConfig.name))).all())
    return {"data": [{"id": item.id, "name": item.name, "provider_type": item.provider_type, "base_url": item.base_url, "model_name": item.model_name, "enabled": item.enabled} for item in items]}
