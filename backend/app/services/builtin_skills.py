import hashlib
import re
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.schemas.skill import SkillWrite


class BuiltinSkillSyncService:
    max_content_length = 24_000
    unrelated = {
        "security-awareness-training", "incident-response", "cloud-security-audit",
        "container-security-testing", "mobile-app-security-testing", "network-penetration-testing",
        "vulnerability-assessment",
    }

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[3] / "configs" / "skills"

    @staticmethod
    def _parse(path: Path) -> tuple[dict, str]:
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---\n"):
            raise ValueError("SKILL.md must start with YAML front matter")
        _, front, content = raw.split("---\n", 2)
        metadata = yaml.safe_load(front) or {}
        if not isinstance(metadata, dict):
            raise ValueError("Skill front matter must be a mapping")
        if len(content) > BuiltinSkillSyncService.max_content_length:
            raise ValueError("Skill content exceeds the configured length limit")
        return metadata, content.strip()

    @staticmethod
    def _challenge_tools(challenge_types: list[str]) -> list[str]:
        if challenge_types == ["TRAFFIC_ANALYSIS"]:
            return ["file_read", "file_search", "python_run", "pcap_metadata", "pcap_protocols", "pcap_query", "pcap_tcp_stream", "pcap_http_objects", "pcap_dns_summary", "pcap_credentials"]
        return ["http_request", "http_session_request", "http_extract", "whatweb_fingerprint", "js_asset_analyze", "source_map_analyze", "file_type", "strings_extract", "archive_list", "file_read", "file_search", "python_run", "content_discovery", "jwt_inspect"]

    @staticmethod
    def _infer_kind(name: str, metadata: dict) -> str:
        if metadata.get("skill_kind") in {"CORE", "METHODOLOGY", "SPECIALIST"}:
            return str(metadata["skill_kind"])
        if name == "ctf-solver-core":
            return "CORE"
        if name.endswith("-methodology"):
            return "METHODOLOGY"
        return "SPECIALIST"

    @staticmethod
    def _infer_activation_mode(skill_kind: str, metadata: dict) -> str:
        value = metadata.get("activation_mode")
        if value in {"ALWAYS", "AUTO", "MANUAL"}:
            return str(value)
        return "ALWAYS" if skill_kind == "CORE" else "AUTO" if skill_kind == "METHODOLOGY" else "MANUAL"

    @staticmethod
    def _infer_triggers(name: str, description: str, metadata: dict, skill_kind: str) -> list[str]:
        values = metadata.get("triggers") or []
        if values:
            return sorted({str(item).strip() for item in values if str(item).strip()})
        tokens = [token for token in re.split(r"[-_\s]+", f"{name} {description}") if token]
        if skill_kind == "CORE":
            return []
        if skill_kind == "METHODOLOGY":
            return sorted({token.lower() for token in tokens[:10]})
        return sorted({token.lower() for token in tokens[:6]})

    @staticmethod
    def _default_phases(challenge_types: list[str]) -> list[str]:
        if challenge_types == ["TRAFFIC_ANALYSIS"]:
            return ["BASELINE", "MAPPING", "TESTING", "FLAG_SEARCH", "FLAG_VERIFICATION", "REPORTING"]
        return [
            "INTAKE",
            "BASELINE",
            "MAPPING",
            "HYPOTHESIS",
            "TESTING",
            "CHAINING",
            "FLAG_SEARCH",
            "FLAG_VERIFICATION",
            "REPORTING",
        ]

    async def sync(self, session: AsyncSession) -> list[str]:
        results: list[str] = []
        if not self.root.exists():
            return results
        for path in sorted(self.root.glob("*/SKILL.md")):
            try:
                relative = path.relative_to(self.root.parent.parent).as_posix()
            except ValueError:
                relative = path.relative_to(self.root).as_posix()
            try:
                metadata, content = self._parse(path)
                challenge_types = list(metadata.get("challenge_types") or ["WEB_TARGET"])
                skill_kind = self._infer_kind(path.parent.name, metadata)
                activation_mode = self._infer_activation_mode(skill_kind, metadata)
                is_unrelated = path.parent.name in self.unrelated
                is_core = path.parent.name == "ctf-solver-core"
                required_tools = list(metadata.get("required_tools") or ([] if is_core else self._challenge_tools(challenge_types)))
                recommended_tools = list(metadata.get("recommended_tools") or ([] if is_core else required_tools))
                forbidden_tools = list(metadata.get("forbidden_tools") or [])
                trigger_metadata = {**metadata, "triggers": metadata.get("positive_triggers") or metadata.get("triggers") or []}
                triggers = self._infer_triggers(path.parent.name, str(metadata.get("description") or ""), trigger_metadata, skill_kind)
                ctf_phases = list(metadata.get("ctf_phases") or self._default_phases(challenge_types))
                payload = SkillWrite(
                    name=str(metadata.get("name") or path.parent.name),
                    display_name=str(
                        metadata.get("display_name") or metadata.get("name") or path.parent.name
                    ),
                    description=str(metadata.get("description") or ""),
                    skill_kind=skill_kind,
                    activation_mode=activation_mode,
                    triggers=triggers,
                    negative_triggers=list(metadata.get("negative_triggers") or []),
                    prerequisites=list(metadata.get("prerequisites") or []),
                    required_tools=required_tools,
                    recommended_tools=recommended_tools,
                    forbidden_tools=forbidden_tools,
                    ctf_phases=ctf_phases,
                    challenge_types=challenge_types,
                    allowed_tools=metadata.get("allowed_tools") or required_tools,
                    risk_level=str(metadata.get("risk_level") or "low"),
                    content_markdown=content,
                    catalog_scope=str(metadata.get("catalog_scope") or ("GENERAL_SECURITY" if is_unrelated else "WEB_CTF")),
                    enabled=bool(metadata.get("enabled", not is_unrelated)),
                )
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                skill = await session.scalar(select(Skill).where(Skill.builtin_path == relative))
                if skill is None:
                    skill = Skill(
                        **payload.model_dump(),
                        source_type="BUILTIN",
                        builtin_path=relative,
                        checksum=checksum,
                    )
                    session.add(skill)
                    results.append(f"created:{relative}")
                elif skill.checksum != checksum:
                    for key, value in payload.model_dump().items():
                        setattr(skill, key, value)
                    skill.version += 1
                    skill.checksum = checksum
                    results.append(f"updated:{relative}")
                if is_unrelated or is_core:
                    skill.enabled = bool(metadata.get("enabled", not is_unrelated))
                    skill.catalog_scope = "GENERAL_SECURITY" if is_unrelated else "WEB_CTF"
                    if is_core:
                        skill.required_tools = []
                        skill.recommended_tools = []
            except (OSError, ValueError, yaml.YAMLError) as error:
                results.append(f"error:{relative}:{error}")
        await session.commit()
        return results


builtin_skill_sync_service = BuiltinSkillSyncService()
