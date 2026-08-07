"""Compact, durable attack-control state.

AttackState deliberately contains no Evidence payload.  Evidence remains in
the EvidenceLedger; this object only describes the current bounded search
space that a Planner is allowed to consume.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AttackState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vulnerability_type: str = "SQL_INJECTION"
    target: dict[str, Any] = Field(default_factory=dict)
    current_phase: str = "HYPOTHESIS"
    current_strategy_family: str = ""
    current_strategy_variant: str = ""
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    failed_strategies: list[str] = Field(default_factory=list)
    blocked_strategies: list[str] = Field(default_factory=list)
    available_actions: list[str] = Field(default_factory=list)
    required_transition: str | None = None
    transition_reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def blocked_actions(self) -> list[str]:
        """Planner-facing alias used by memory snapshots."""
        return list(self.blocked_strategies)

    def planner_view(self) -> dict[str, Any]:
        return {
            "current_phase": self.current_phase,
            "available_actions": list(self.available_actions),
            "blocked_actions": list(self.blocked_strategies),
            "transition_reason": self.transition_reason,
            "required_transition": self.required_transition,
        }
