from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class SolverRole:
    name: str
    version: str
    display_name: str
    description: str
    challenge_types: list[str]
    tools: list[str]
    skills: list[str]
    limits: dict
    system_rules: list[str]
    source_path: str

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "challenge_types": self.challenge_types,
            "tools": self.tools,
            "skills": self.skills,
            "limits": self.limits,
            "system_rules": self.system_rules,
            "source_path": self.source_path,
        }


class RoleLoader:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[3] / "configs" / "roles"

    def load(self, challenge_type: str) -> SolverRole:
        name = "traffic-ctf-solver" if challenge_type == "TRAFFIC_ANALYSIS" else "web-ctf-solver"
        path = self.root / f"{name}.yaml"
        metadata = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(metadata, dict):
            raise ValueError("Role definition must be a mapping")
        try:
            source_path = path.relative_to(self.root.parent.parent).as_posix()
        except ValueError:
            source_path = path.relative_to(self.root).as_posix()
        return SolverRole(
            name=str(metadata.get("name") or name),
            version=str(metadata.get("version") or "1"),
            display_name=str(metadata.get("display_name") or name),
            description=str(metadata.get("description") or ""),
            challenge_types=list(metadata.get("challenge_types") or [challenge_type]),
            tools=list(metadata.get("tools") or []),
            skills=list(metadata.get("skills") or []),
            limits=dict(metadata.get("limits") or {}),
            system_rules=[str(item) for item in (metadata.get("system_rules") or [])],
            source_path=source_path,
        )


role_loader = RoleLoader()
