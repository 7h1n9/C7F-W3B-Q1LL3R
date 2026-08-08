from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..phases import MutekiPhase


@dataclass(frozen=True, slots=True)
class StagePolicy:
    """Explicit worker-role and transition policy for canonical Muteki stages."""

    rules: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "prepare": {"max_workers": 0, "allowed_roles": []},
        "race": {"max_workers": 10, "allowed_roles": ["race"]},
        "coordinator": {"max_workers": 10, "allowed_roles": ["bootstrap", "explore", "review"]},
        "finalize": {"max_workers": 0, "allowed_roles": []},
    })

    @classmethod
    def from_config(cls, value: Any = None) -> "StagePolicy":
        if isinstance(value, cls):
            return value
        policy = cls()
        if not isinstance(value, dict):
            return policy
        merged = {stage: dict(rule) for stage, rule in policy.rules.items()}
        for stage, rule in value.items():
            if stage in merged and isinstance(rule, dict):
                merged[stage].update(rule)
        return cls(merged)

    def get_max_workers(self, stage: str | MutekiPhase) -> int:
        rule = self.rules.get(str(stage), {})
        return max(0, int(rule.get("max_workers", 0)))

    def get_allowed_roles(self, stage: str | MutekiPhase) -> tuple[str, ...]:
        rule = self.rules.get(str(stage), {})
        return tuple(str(role) for role in rule.get("allowed_roles", ()) if str(role))

    def can_spawn(self, stage: str | MutekiPhase, role: str) -> bool:
        return role in self.get_allowed_roles(stage)

    def can_transition(self, from_stage: str | MutekiPhase, to_stage: str | MutekiPhase) -> bool:
        source, target = str(from_stage), str(to_stage)
        transitions = {
            "prepare": {"race", "finalize"},
            "race": {"coordinator", "finalize"},
            "coordinator": {"coordinator", "finalize"},
            "finalize": {"finalize"},
        }
        return target in transitions.get(source, set())


__all__ = ["StagePolicy"]
