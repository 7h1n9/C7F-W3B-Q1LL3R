"""Typed benchmark case definitions."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    vulnerability_type: str
    target: str
    initial_knowledge: list[str] = Field(default_factory=list)
    attack_surface: list[dict[str, Any]] = Field(default_factory=list)
    expected_hypothesis: dict[str, Any] = Field(default_factory=dict)
    expected_validation: dict[str, Any] = Field(default_factory=dict)
    expected_exploit: dict[str, Any] = Field(default_factory=dict)
    expected_impact: dict[str, Any] = Field(default_factory=dict)
    expected_finding: dict[str, Any] = Field(default_factory=dict)
    # Interaction budget for one attempt, in agent turns.  CTF-Dojo reports its
    # results under a per-challenge turn budget (~40 turns), so a case that wants
    # to be comparable to that number must declare one; ``None`` means the case
    # is scored without a budget and ``solved_within_budget`` stays unknown.
    turn_budget: int | None = Field(default=None, ge=1)
    # The range this case needs, when it is not the repo's default one.  Naming
    # it turns "this case quietly fails every repetition" into an explicit skip,
    # so a suite cannot be read as a low solve rate when the target never existed.
    requires_range: str | None = None
    # Technology vocabulary this case exercises (flask / mysql / php ...), using
    # the same words the curated knowledge corpus uses.  It is what lets lessons
    # from one solved challenge be offered to another of the same shape instead
    # of every case of a vulnerability type being pooled together.
    target_stack: list[str] = Field(default_factory=list)
