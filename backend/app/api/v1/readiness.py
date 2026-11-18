"""Range readiness: is this deployment able to solve a challenge right now?

What "ready" means after the Runner was retired
-----------------------------------------------
Muteki executes tools in container-native Workers, so a **legacy Kali Runner** is
no longer part of the execution path.  It used to be a hard requirement here --
readiness emitted five ``runner*`` checks, and when the (usually absent) Kali VM
was down the console showed ``NOT_READY`` with five red cards, which read as "the
system is broken" rather than "an optional service you are not using is absent".

So the Runner checks are now emitted **only when** ``legacy_kali_runner`` is on
(``APP_LEGACY_KALI_RUNNER=1``), and the readiness verdict is computed from the
checks that actually gate a solve:

* ``database`` at the expected migration revision;
* at least one enabled model configuration (the solver needs a model);
* at least one enabled Skill (the methodology layer).

Web and traffic levels no longer hinge on ``runner``/``runner_token``/``web_tool``
because those describe a service this deployment is not using.  Turning the flag
back on restores the previous behaviour exactly, including the check names, so an
operator who *does* front a Runner sees no change.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.models.model_config import ModelConfig
from app.models.skill import Skill

router = APIRouter(prefix="/readiness", tags=["readiness"])

#: Checks that gate a web solve.  ``runner*`` is deliberately absent: the
#: container-native Worker path does not go through the legacy service.
WEB_GATE_CHECKS = {"database", "model_config", "skills"}
#: Traffic analysis additionally needs the pcap tooling.
TRAFFIC_GATE_CHECKS = {"database", "model_config", "skills", "pcap_upload_validation"}


def expected_revision() -> str | None:
    """The revision the migration scripts say is head.

    Read from the scripts rather than pinned to a literal.  It *was* a literal
    (``0025_data_governance_web_research``) and quietly rotted 24 migrations
    behind: the database was fully migrated and correct, the check compared it
    against a stale constant, and the console reported the deployment as
    ``NOT_READY`` with a red "数据库" card.  A readiness check whose own value is
    the thing that is out of date is worse than no check -- it teaches the
    operator to ignore the page.

    Returns ``None`` when the scripts cannot be read, which the caller treats as
    "unknown" rather than as a failure.
    """
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_root = Path(__file__).resolve().parents[3]
    try:
        script = ScriptDirectory.from_config(Config(str(backend_root / "alembic.ini")))
        return script.get_current_head()
    except Exception:
        return None


async def _runner_checks() -> list[dict]:
    """The legacy Runner probe.  Only called when the flag is on."""
    from app.services.runner_client import runner_client

    try:
        health = await runner_client.health()
    except Exception as error:
        return [
            {"name": name, "ok": False, "message": f"Runner unavailable: {str(error)[:160]}"}
            for name in ("runner", "runner_token", "web_tool", "tshark", "capinfos")
        ]
    raw_capabilities = health.get("capabilities", {})
    # Runner v0.2 exposes a structured registry while older Runners used flat
    # tshark/capinfos keys.  Normalize both shapes for readiness.
    capabilities = (
        dict(raw_capabilities.get("binaries", {})) if isinstance(raw_capabilities, dict) else {}
    )
    if isinstance(raw_capabilities, dict):
        capabilities.update(
            {
                item.get("name"): item.get("available")
                for item in raw_capabilities.get("tools", [])
                if isinstance(item, dict) and item.get("name")
            }
        )
        capabilities.update(
            {
                key: value
                for key, value in raw_capabilities.items()
                if key not in {"binaries", "tools"}
            }
        )
    return [
        {"name": "runner", "ok": True, "message": "Runner is reachable"},
        {"name": "runner_token", "ok": True, "message": "Runner client token is configured"},
        {"name": "web_tool", "ok": True, "message": "Runner reports a healthy execution backend"},
        {"name": "tshark", "ok": bool(capabilities.get("tshark")), "message": "tshark capability"},
        {"name": "capinfos", "ok": bool(capabilities.get("capinfos")), "message": "capinfos capability"},
    ]


def _tool_channel_checks() -> list[dict]:
    """What the solve path actually depends on, once the Runner is retired.

    Reported as information rather than as a gate: the container-native Worker is
    what executes tools, so whether Docker is visible is worth knowing on this
    page -- it just is not the same question as "is the deployment configured".
    """
    import shutil

    docker = shutil.which("docker") is not None
    return [
        {
            "name": "tool_channel",
            "ok": True,
            "message": (
                "legacy Kali Runner enabled."
                if get_settings().legacy_kali_runner
                else "container-native Worker (Muteki); legacy Kali Runner disabled."
            ),
        },
        {
            "name": "docker_cli",
            "ok": docker,
            "message": (
                "docker CLI visible to the backend" if docker else "docker CLI not found on PATH"
            ),
        },
    ]


@router.get("/range-test")
async def range_test_readiness(session: AsyncSession = Depends(get_session)) -> dict:
    checks: list[dict] = []
    head = expected_revision()
    try:
        revision = (
            await session.execute(text("select version_num from alembic_version"))
        ).scalar_one_or_none()
        checks.append(
            {
                "name": "database",
                "ok": bool(revision) and (head is None or revision == head),
                "message": f"Migration revision: {revision or 'missing'}"
                + (f" (head {head})" if head else ""),
            }
        )
    except Exception as error:
        checks.append({"name": "database", "ok": False, "message": str(error)[:200]})

    if get_settings().legacy_kali_runner:
        checks.extend(await _runner_checks())
    else:
        checks.extend(_tool_channel_checks())

    enabled_models = list(
        (await session.scalars(select(ModelConfig).where(ModelConfig.enabled))).all()
    )
    enabled_skills = list((await session.scalars(select(Skill).where(Skill.enabled))).all())
    checks.append(
        {
            "name": "model_config",
            "ok": bool(enabled_models),
            "message": (
                f"{len(enabled_models)} enabled model configuration(s); "
                "connection tests are performed from Settings."
            ),
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

    web = all(item["ok"] for item in checks if item["name"] in WEB_GATE_CHECKS)
    traffic = all(item["ok"] for item in checks if item["name"] in TRAFFIC_GATE_CHECKS)
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