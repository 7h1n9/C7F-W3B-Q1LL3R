from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlackboardState(BaseModel):
    """Durable Solver control state, separate from Evidence Store records."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    run_id: str
    version: int = 0
    phase: str = "BASELINE"
    goal: str | dict[str, Any] = ""
    knowledge: dict[str, Any] = Field(default_factory=dict)
    control: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @property
    def facts(self) -> list[dict[str, Any]]:
        """Phase 1.1 compatibility view over the knowledge summary."""
        return list(self.knowledge.get("facts") or [])

    @property
    def hypotheses(self) -> list[dict[str, Any]]:
        """Phase 1.1 compatibility view over the knowledge summary."""
        return list(self.knowledge.get("hypotheses") or [])

    @property
    def allowed_actions(self) -> list[str]:
        """Phase 1.1 compatibility view over the control summary."""
        return [str(item) for item in (self.control.get("allowed_actions") or [])]

    def copy_for_read(self) -> "BlackboardState":
        """Return a detached snapshot so callers cannot mutate stored state."""
        return self.model_copy(deep=True)
