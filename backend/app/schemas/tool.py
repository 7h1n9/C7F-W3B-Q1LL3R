from typing import Literal

from pydantic import BaseModel, Field


class ToolInvoke(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict


class ToolModelView(BaseModel):
    summary: str = ""
    content_excerpt: str | None = None
    extracted_facts: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    suggested_next_dimensions: list[str] = Field(default_factory=list)


class ToolArtifactRef(BaseModel):
    artifact_id: str
    relative_path: str
    sha256: str
    size: int
    mime_type: str


class ToolExecutionResult(BaseModel):
    status: Literal["COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"]
    model_view: ToolModelView
    artifacts: list[ToolArtifactRef] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    error_details: dict = Field(default_factory=dict)
