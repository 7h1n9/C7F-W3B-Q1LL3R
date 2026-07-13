from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionHypothesis(BaseModel):
    id: str | None = None
    category: str = "GENERAL"
    statement: str = Field(min_length=1, max_length=2000)
    confidence: int = Field(default=20, ge=0, le=100)
    supporting_fact_ids: list[str] = Field(default_factory=list)


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["tool"]
    phase: str = "INTAKE"
    objective: str = "Continue the authorized investigation"
    hypothesis: ActionHypothesis | str = "Initial investigation hypothesis"
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict
    reason: str = Field(min_length=1, max_length=2000)
    hypothesis_id: str | None = None
    expected_evidence: str = "A new, reviewable observation or artifact"
    success_condition: str = "The tool result confirms or rejects the hypothesis"
    failure_pivot: str = "Record the failure and choose a different bounded dimension"
    retry_reason: str | None = None
    activate_skill: str | None = None


class SkillAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["skill"]
    operation: Literal["activate", "deactivate", "inspect", "decline"]
    phase: str
    objective: str
    reason: str = Field(min_length=1, max_length=2000)
    skill_id: str | None = Field(default=None, max_length=100)
    skill_name: str | None = Field(default=None, max_length=200)
    supporting_evidence: list[str] = Field(default_factory=list)
    expected_use: str = Field(min_length=1, max_length=2000)


class FinishAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["finish"]
    phase: str | None = None
    objective: str | None = None
    hypothesis: str | None = None
    result: Literal["solved", "unsolved", "waiting_user"]
    summary: str = Field(min_length=1, max_length=4000)
    flag_candidate: str | None = Field(default=None, max_length=1000)
    expected_evidence: str | None = None
    success_condition: str | None = None
    failure_pivot: str | None = None


AgentAction = Annotated[ToolAction | SkillAction | FinishAction, Field(discriminator="type")]
