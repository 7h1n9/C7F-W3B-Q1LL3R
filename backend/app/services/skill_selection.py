from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.skill import ChallengeSkillBinding, ModelSkillBinding, RunSkillSnapshot, Skill

MAX_SKILL_CONTEXT = 48_000


async def snapshot_run_skills(
    session: AsyncSession,
    run_id: str,
    challenge_id: str,
    challenge_type: str,
    model_config_id: str | None,
    selected_ids: list[str],
    disabled_ids: list[str],
) -> list[RunSkillSnapshot]:
    candidates: dict[str, tuple[int, dict, Skill]] = {}
    if model_config_id:
        bindings = list(
            (
                await session.scalars(
                    select(ModelSkillBinding).where(
                        ModelSkillBinding.model_config_id == model_config_id,
                        ModelSkillBinding.enabled,
                    )
                )
            ).all()
        )
        for binding in bindings:
            skill = await session.get(Skill, binding.skill_id)
            if skill and skill.enabled:
                candidates[skill.id] = (binding.priority, binding.config_json, skill)
    bindings = list(
        (
            await session.scalars(
                select(ChallengeSkillBinding).where(
                    ChallengeSkillBinding.challenge_id == challenge_id
                )
            )
        ).all()
    )
    for binding in bindings:
        skill = await session.get(Skill, binding.skill_id)
        if skill and skill.enabled:
            old = candidates.get(skill.id)
            candidates[skill.id] = (
                min(binding.priority, old[0]) if old else binding.priority,
                old[1] if old else {},
                skill,
            )
    for index, skill_id in enumerate(selected_ids):
        skill = await session.get(Skill, skill_id)
        if not skill or not skill.enabled:
            raise DomainError(
                "SKILL_NOT_AVAILABLE", "Selected Skill is unavailable.", {"skill_id": skill_id}, 422
            )
        candidates[skill_id] = (
            candidates.get(skill_id, (1000 + index, {}, skill))[0],
            candidates.get(skill_id, (0, {}, skill))[1],
            skill,
        )
    for skill_id in disabled_ids:
        candidates.pop(skill_id, None)
    auto_skills = [
        skill
        for skill in (
            await session.scalars(
                select(Skill).where(Skill.enabled, Skill.skill_kind.in_(["CORE", "METHODOLOGY"]))
            )
        ).all()
        if (skill.skill_kind == "CORE" or challenge_type in (skill.challenge_types or []))
        and skill.id not in disabled_ids
    ]
    for skill in auto_skills:
        candidates.setdefault(skill.id, (0, {}, skill))
    snapshots, length = [], 0
    for skill_id, (priority, config, skill) in sorted(
        candidates.items(), key=lambda row: (row[1][0], row[1][2].name)
    ):
        if length + len(skill.content_markdown) > MAX_SKILL_CONTEXT:
            continue
        length += len(skill.content_markdown)
        snapshot = RunSkillSnapshot(
            run_id=run_id,
            skill_id=skill_id,
            skill_name=skill.name,
            skill_version=skill.version,
            content_snapshot=skill.content_markdown,
            allowed_tools_snapshot=skill.allowed_tools,
            config_snapshot=config,
            priority=priority,
        )
        session.add(snapshot)
        snapshots.append(snapshot)
    return snapshots


def allowed_tools_for(challenge_type: str) -> set[str]:
    return (
        {
            "http_request", "http_session_request", "http_extract", "whatweb_fingerprint",
            "js_asset_analyze", "source_map_analyze", "file_type", "strings_extract", "archive_list",
            "content_discovery", "jwt_inspect", "sqlmap_detect", "nmap_service_probe", "nikto_scan",
            "binwalk_scan", "exiftool_metadata", "file_read", "file_search", "python_run", "script_run", "sandbox_exec",
        }
        if challenge_type == "WEB_TARGET"
        else {
            "file_read",
            "file_search",
            "python_run",
            "script_run",
            "sandbox_exec",
            "pcap_metadata",
            "pcap_protocols",
            "pcap_query",
            "pcap_tcp_stream",
            "pcap_http_objects",
            "pcap_dns_summary",
            "pcap_credentials",
        }
    )


async def specialist_skill_catalog(
    session: AsyncSession, challenge_type: str, disabled_ids: set[str] | None = None
) -> list[Skill]:
    disabled = disabled_ids or set()
    items = list(
        (
            await session.scalars(select(Skill).where(Skill.enabled, Skill.skill_kind == "SPECIALIST"))
        ).all()
    )
    return [item for item in items if challenge_type in (item.challenge_types or []) and item.id not in disabled]
