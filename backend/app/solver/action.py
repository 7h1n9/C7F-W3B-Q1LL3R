from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionIntent:
    """A bounded, policy-checkable intention from Planner to Worker."""

    action_name: str
    reason: str
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def action(self) -> str:
        """Phase 1.1 compatibility alias; new code should use action_name."""
        return self.action_name
