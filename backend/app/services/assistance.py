"""Assistance and assumption provenance rules for solver evaluations."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

ASSISTANCE_LEVELS = {"AUTONOMOUS", "HINT_GUIDED", "EVIDENCE_GUIDED", "ANSWER_GUIDED"}
ASSUMPTION_SOURCES = {"OBSERVATION", "MODEL_INFERENCE", "USER_HINT", "KNOWN_ANSWER"}


class Assumption(BaseModel):
    value: str = Field(min_length=1, max_length=4000)
    source: str
    evidence_id: str | None = None

    @field_validator("source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if value not in ASSUMPTION_SOURCES:
            raise ValueError(f"unknown assumption source: {value}")
        return value


class ScriptDesignCard(BaseModel):
    vulnerability_class: str = Field(min_length=1, max_length=120)
    confirmed_oracle: dict[str, Any] = Field(default_factory=dict)
    transport: dict[str, Any] = Field(default_factory=dict)
    dbms: dict[str, Any] = Field(default_factory=dict)
    target_data: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[Assumption] = Field(default_factory=list)
    estimated_requests: int = Field(default=0, ge=0)
    max_requests: int = Field(default=1, ge=1)
    stop_conditions: list[str] = Field(default_factory=list)
    fallback: str | None = None


def assistance_level(assumptions: list[dict[str, Any]] | list[Assumption] | None) -> str:
    sources = {item.source if isinstance(item, Assumption) else item.get("source") for item in (assumptions or [])}
    sources.discard(None)
    if "KNOWN_ANSWER" in sources:
        return "ANSWER_GUIDED"
    if sources == {"USER_HINT"}:
        return "HINT_GUIDED"
    if sources:
        return "EVIDENCE_GUIDED"
    return "AUTONOMOUS"


def classify_user_input(content: str) -> str:
    """Classify user guidance without treating it as solver evidence."""
    text = str(content or "")
    if not text.strip():
        return "AUTONOMOUS"
    if re.search(r"(?i)flag\{[^{}\r\n]{1,256}\}|(?:known answer|answer is|solution is)", text):
        return "ANSWER_GUIDED"
    return "HINT_GUIDED"


def validate_design_card(card: dict[str, Any]) -> tuple[ScriptDesignCard, str]:
    parsed = ScriptDesignCard.model_validate(card)
    if parsed.estimated_requests > parsed.max_requests:
        raise ValueError("estimated_requests cannot exceed max_requests")
    return parsed, assistance_level(parsed.assumptions)
