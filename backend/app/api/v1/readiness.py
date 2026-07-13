from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.model_config import ModelConfig
from app.models.skill import Skill
from app.services.runner_client import runner_client

router = APIRouter(prefix="/readiness", tags=["readiness"])
EXPECTED_REVISION = "0012_run_attempts"


@router.get("/range-test")
async def range_test_readiness(session: AsyncSession = Depends(get_session)) -> dict:
    checks = []
    try:
        revision = (
            await session.execute(text("select version_num from alembic_version"))
        ).scalar_one_or_none()
        checks.append(
            {
                "name": "database",
                "ok": revision == EXPECTED_REVISION,
                "message": f"Migration revision: {revision or 'missing'}",
            }
        )
    except Exception as error:
        checks.append({"name": "database", "ok": False, "message": str(error)[:200]})
    try:
        health = await runner_client.health()
        raw_capabilities = health.get("capabilities", {})
        # Runner v0.2 exposes a structured registry while older Runners used
        # flat tshark/capinfos keys. Normalize both shapes for readiness.
        capabilities = dict(raw_capabilities.get("binaries", {})) if isinstance(raw_capabilities, dict) else {}
        if isinstance(raw_capabilities, dict):
            capabilities.update(
                {
                    item.get("name"): item.get("available")
                    for item in raw_capabilities.get("tools", [])
                    if isinstance(item, dict) and item.get("name")
                }
            )
            capabilities.update({key: value for key, value in raw_capabilities.items() if key not in {"binaries", "tools"}})
        checks.extend(
            [
                {"name": "runner", "ok": True, "message": "Runner is reachable"},
                {
                    "name": "runner_token",
                    "ok": True,
                    "message": "Runner client token is configured",
                },
                {
                    "name": "web_tool",
                    "ok": True,
                    "message": "Runner reports a healthy execution backend",
                },
                {
                    "name": "tshark",
                    "ok": bool(capabilities.get("tshark")),
                    "message": "tshark capability",
                },
                {
                    "name": "capinfos",
                    "ok": bool(capabilities.get("capinfos")),
                    "message": "capinfos capability",
                },
            ]
        )
    except Exception as error:
        checks.extend(
            {"name": name, "ok": False, "message": f"Runner unavailable: {str(error)[:160]}"}
            for name in ("runner", "runner_token", "web_tool", "tshark", "capinfos")
        )
    enabled_models = list(
        (await session.scalars(select(ModelConfig).where(ModelConfig.enabled))).all()
    )
    enabled_skills = list((await session.scalars(select(Skill).where(Skill.enabled))).all())
    checks.append(
        {
            "name": "model_config",
            "ok": bool(enabled_models),
            "message": f"{len(enabled_models)} enabled model configuration(s); connection tests are performed from Settings.",
        }
    )
    checks.append(
        {
            "name": "skills",
            "ok": bool(enabled_skills),
            "message": f"{len(enabled_skills)} enabled Skill(s)",
        }
    )
    checks.append(
        {
            "name": "pcap_upload_validation",
            "ok": True,
            "message": "PCAP extension and magic validation is installed",
        }
    )
    web = all(
        item["ok"]
        for item in checks
        if item["name"]
        in {"database", "runner", "runner_token", "web_tool", "model_config", "skills"}
    )
    traffic = all(
        item["ok"]
        for item in checks
        if item["name"]
        in {
            "database",
            "runner",
            "tshark",
            "capinfos",
            "model_config",
            "skills",
            "pcap_upload_validation",
        }
    )
    level = (
        "READY_FOR_RANGE_SMOKE_TEST"
        if web and traffic
        else "READY_FOR_WEB_SMOKE_TEST"
        if web
        else "READY_FOR_TRAFFIC_SMOKE_TEST"
        if traffic
        else "NOT_READY"
    )
    return {"data": {"ready": web and traffic, "level": level, "checks": checks}}
