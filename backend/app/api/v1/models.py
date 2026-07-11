import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.model_config import ModelConfig
from app.schemas.model_config import ModelConfigUpdate, ModelConfigWrite
from app.services.crypto import decrypt_api_key, encrypt_api_key

router = APIRouter(prefix="/model-configs", tags=["settings"])


def read(item: ModelConfig) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "provider_type": item.provider_type,
        "base_url": item.base_url,
        "model_name": item.model_name,
        "enabled": item.enabled,
        "api_key_configured": bool(item.encrypted_api_key),
    }


@router.get("")
async def list_model_configs(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(ModelConfig).order_by(ModelConfig.name))).all())
    return {"data": [read(item) for item in items]}


@router.post("", status_code=201)
async def create_model_config(
    payload: ModelConfigWrite, session: AsyncSession = Depends(get_session)
) -> dict:
    item = ModelConfig(
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=str(payload.base_url).rstrip("/"),
        model_name=payload.model_name,
        encrypted_api_key=encrypt_api_key(payload.api_key or ""),
        enabled=payload.enabled,
    )
    session.add(item)
    await session.commit()
    return {"data": read(item)}


@router.put("/{config_id}")
async def update_model_config(
    config_id: str, payload: ModelConfigUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        from app.core.exceptions import DomainError

        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    item.name, item.provider_type, item.base_url, item.model_name, item.enabled = (
        payload.name,
        payload.provider_type,
        str(payload.base_url).rstrip("/"),
        payload.model_name,
        payload.enabled,
    )
    if payload.api_key:
        item.encrypted_api_key = encrypt_api_key(payload.api_key)
    await session.commit()
    return {"data": read(item)}


@router.delete("/{config_id}", status_code=204)
async def delete_model_config(config_id: str, session: AsyncSession = Depends(get_session)) -> None:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        from app.core.exceptions import DomainError

        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    await session.delete(item)
    await session.commit()


@router.post("/{config_id}/test")
async def test_model_config(config_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        from app.core.exceptions import DomainError

        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                f"{item.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {decrypt_api_key(item.encrypted_api_key)}"},
                json={
                    "model": item.model_name,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 4,
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as error:
        return {"data": {"ok": False, "message": "Model connection failed", "details": str(error)}}
    return {"data": {"ok": True, "message": "Model connection succeeded"}}
