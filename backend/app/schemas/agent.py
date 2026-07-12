from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["tool"]
    phase: str | None = None
    objective: str | None = None
    hypothesis: str | None = None
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict
    reason: str = Field(min_length=1, max_length=2000)
    hypothesis_id: str | None = None
    expected_evidence: str | None = None
    success_condition: str | None = None
    failure_pivot: str | None = None
    retry_reason: str | None = None
    activate_skill: str | None = None


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


AgentAction = Annotated[ToolAction | FinishAction, Field(discriminator="type")]
