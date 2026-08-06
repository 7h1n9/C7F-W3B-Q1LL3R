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
