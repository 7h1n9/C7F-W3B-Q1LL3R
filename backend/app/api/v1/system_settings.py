import httpx
from fastapi import APIRouter

from app.core.config import get_settings, persist_service_urls
from app.schemas.system_settings import ServiceSettingsUpdate

router = APIRouter(prefix="/system-settings", tags=["settings"])


async def probe(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
            response.raise_for_status()
        return {"reachable": True, "details": response.json()}
    except (httpx.HTTPError, ValueError) as error:
        return {"reachable": False, "details": str(error)}


async def read() -> dict:
    settings = get_settings()
    runner, bridge = await probe(settings.runner_url), await probe(settings.codex_bridge_url)
    return {
        "runner_url": settings.runner_url,
        "runner_token_configured": bool(settings.runner_api_token),
        "runner": runner,
        "codex_bridge_url": settings.codex_bridge_url,
        "codex_bridge": bridge,
    }


@router.get("")
async def get_system_settings() -> dict:
    return {"data": await read()}


@router.put("")
async def update_system_settings(payload: ServiceSettingsUpdate) -> dict:
    runner_url, bridge_url = str(payload.runner_url).rstrip("/"), str(payload.codex_bridge_url).rstrip("/")
    persist_service_urls(runner_url, bridge_url)
    settings = get_settings()
    settings.runner_url, settings.codex_bridge_url = runner_url, bridge_url
    return {"data": await read()}
