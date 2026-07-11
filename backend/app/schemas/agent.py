from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["tool"]
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict
    reason: str = Field(min_length=1, max_length=2000)
    hypothesis_id: str | None = None


class FinishAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["finish"]
    result: Literal["solved", "unsolved", "waiting_user"]
    summary: str = Field(min_length=1, max_length=4000)
    flag_candidate: str | None = Field(default=None, max_length=1000)


AgentAction = Annotated[ToolAction | FinishAction, Field(discriminator="type")]
