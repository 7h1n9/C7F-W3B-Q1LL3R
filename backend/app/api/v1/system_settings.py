import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, persist_default_model, persist_service_urls
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.model_config import ModelConfig
from app.schemas.system_settings import ServiceSettingsUpdate

router = APIRouter(prefix="/system-settings", tags=["settings"])


async def probe(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
            response.raise_for_status()
        return {"reachable": True, "details": response.json()}
    except (httpx.HTTPError, ValueError) as error:
        return {"reachable": False, "details": str(error)}


async def _resolve_default_model(session: AsyncSession, model_config_id: str | None) -> dict:
    """Resolve the global default into what the UI needs to render it.

    An id that no longer resolves is reported as unset with an explanatory label
    rather than dropped silently: the value is still stored, and hiding that a
    configured default went missing would make the next solve's failure look
    unrelated.
    """
    if not model_config_id:
        return {"default_model_config_id": None, "default_model_label": None}
    config = await session.get(ModelConfig, model_config_id)
    if config is None:
        return {
            "default_model_config_id": model_config_id,
            "default_model_label": "（已删除的模型配置）",
        }
    if not config.enabled:
        return {
            "default_model_config_id": model_config_id,
            "default_model_label": f"{config.name}（已禁用）",
        }
    return {"default_model_config_id": model_config_id, "default_model_label": config.name}


async def read(session: AsyncSession) -> dict:
    settings = get_settings()
    default_model = await _resolve_default_model(session, settings.default_model_config_id)
    # Probing the legacy Runner is gated on it actually being part of the
    # deployment.  The probe is a hard 15s outbound call, and with the Runner
    # retired it was 15 seconds of every settings page load spent asking an
    # absent service whether it was absent -- then reporting the answer as a red
    # "unreachable" beside the address that no longer matters.
    if settings.legacy_kali_runner:
        runner = await probe(settings.runner_url)
    else:
        runner = {"reachable": True, "details": "legacy Kali Runner disabled; container-native Worker"}
    return {
        "runner_url": settings.runner_url,
        "runner_allowed_cidrs": settings.allowed_service_cidrs,
        "runner_token_configured": bool(settings.runner_api_token),
        "runner": runner,
        "legacy_kali_runner": settings.legacy_kali_runner,
        **default_model,
    }


@router.get("")
async def get_system_settings(session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": await read(session)}


@router.put("")
async def update_system_settings(
    payload: ServiceSettingsUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    settings = get_settings()
    # A blank Runner URL keeps the stored one.  With the legacy Runner retired the
    # settings dialog no longer offers the field, so requiring it would make an
    # unrelated edit (the global default model) fail validation against a service
    # the deployment does not use.
    runner_url = (
        str(payload.runner_url).rstrip("/") if payload.runner_url is not None else settings.runner_url
    )
    model_config_id = payload.default_model_config_id
    # The schema cannot reach the DB, so the existence/enabled contract is
    # enforced here, before anything is persisted.
    if model_config_id is not None:
        config = await session.get(ModelConfig, model_config_id)
        if config is None:
            raise DomainError(
                "SYSTEM_DEFAULT_MODEL_NOT_FOUND",
                "The selected default model configuration does not exist.",
                status_code=422,
            )
        if not config.enabled:
            raise DomainError(
                "SYSTEM_DEFAULT_MODEL_DISABLED",
                "The selected default model configuration is disabled.",
                status_code=422,
            )
    persist_service_urls(runner_url)
    persist_default_model(model_config_id)
    settings.runner_url = runner_url
    settings.default_model_config_id = model_config_id
    return {"data": await read(session)}