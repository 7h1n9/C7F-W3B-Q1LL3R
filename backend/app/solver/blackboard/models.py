from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlackboardState(BaseModel):
    """Minimal solver-control snapshot.

    This is deliberately not a replacement for the existing evidence or
    security persistence models.  Evidence and artifact fields are references
    or reduced facts only; raw records remain owned by their existing stores.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    version: int = 0
    phase: str = "BASELINE"
    allowed_actions: list[str] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)

    def copy_for_read(self) -> "BlackboardState":
        """Return a detached snapshot so callers cannot mutate the store."""
        return self.model_copy(deep=True)
