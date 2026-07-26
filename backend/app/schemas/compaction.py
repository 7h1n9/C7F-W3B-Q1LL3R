from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CompactionDecisionAction(BaseModel):
    """Model output for semantic selection only; it has no delete operation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["compaction_decision"] = "compaction_decision"
    canonical_facts: list[dict] = Field(default_factory=list)
    confirmed_capabilities: list[dict] = Field(default_factory=list)
    active_hypotheses: list[dict] = Field(default_factory=list)
    rejected_hypotheses: list[dict] = Field(default_factory=list)
    attack_chain_summary: list[dict] = Field(default_factory=list)
    current_exploit_plan: dict = Field(default_factory=dict)
    keep_tool_call_ids: list[str] = Field(default_factory=list)
    keep_observation_ids: list[str] = Field(default_factory=list)
    keep_artifact_ids: list[str] = Field(default_factory=list)
    keep_event_ids: list[str] = Field(default_factory=list)
    wp_critical_evidence_ids: list[str] = Field(default_factory=list)
    automation_outputs: list[str] = Field(default_factory=list)
    script_paths: list[str] = Field(default_factory=list)
    recent_failures: list[dict] = Field(default_factory=list)
    next_actions: list[dict] = Field(default_factory=list)
    compaction_reason: str = Field(default="threshold_reached", max_length=2000)


def empty_evidence_snapshot(generation: int) -> dict:
    return {
        "generation": generation,
        "canonical_facts": [],
        "confirmed_capabilities": [],
        "active_hypotheses": [],
        "rejected_paths": [],
        "attack_chain": [],
        "current_exploit_plan": {},
        "automation_state": {},
        "scripts": [],
        "critical_artifacts": [],
        "wp_critical_steps": [],
        "recent_errors": [],
        "next_actions": [],
    }
