import hashlib
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.schemas.skill import SkillWrite


class BuiltinSkillSyncService:
    max_content_length = 24_000

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

    async def sync(self, session: AsyncSession) -> list[str]:
        results: list[str] = []
        if not self.root.exists():
            return results
        for path in sorted(self.root.glob("*/SKILL.md")):
            relative = path.relative_to(self.root.parent.parent).as_posix()
            try:
                metadata, content = self._parse(path)
                payload = SkillWrite(
                    name=str(metadata.get("name") or path.parent.name),
                    display_name=str(
                        metadata.get("display_name") or metadata.get("name") or path.parent.name
                    ),
                    description=str(metadata.get("description") or ""),
                    challenge_types=metadata.get("challenge_types") or ["WEB_TARGET"],
                    allowed_tools=metadata.get("allowed_tools") or [],
                    risk_level=str(metadata.get("risk_level") or "low"),
                    content_markdown=content,
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
            except (OSError, ValueError, yaml.YAMLError) as error:
                results.append(f"error:{relative}:{error}")
        await session.commit()
        return results


builtin_skill_sync_service = BuiltinSkillSyncService()
