from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    engine_type: str = Field(default="mock", pattern="^(mock|openai_compatible|codex_sdk)$")
    model_config_id: str | None = None
    max_agent_steps: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=12, ge=0, le=100)
    max_context_observations: int = Field(default=8, ge=1, le=50)
    max_runtime_seconds: int = Field(default=300, ge=10, le=3600)
    selected_skill_ids: list[str] = Field(default_factory=list, max_length=30)
    disabled_skill_ids: list[str] = Field(default_factory=list, max_length=30)
    conversation_id: str | None = None


class RunRead(BaseModel):
    id: str
    challenge_id: str
    challenge_name: str | None = None
    challenge_type: str | None = None
    target_summary: str | None = None
    engine_type: str
    model_config_id: str | None
    model_name: str | None = None
    role_name: str | None
    role_version: str | None
    role_snapshot_json: dict
    status: str
    current_phase: str
    workspace_path: str
    codex_thread_id: str | None
    max_agent_steps: int
    max_tool_calls: int
    max_context_observations: int
    max_runtime_seconds: int
    agent_step_count: int
    tool_call_count: int
    last_error_code: str | None
    last_error_message: str | None
    active_skill_names: list[str] = Field(default_factory=list)
    diagnostic_tags: list[str] = Field(default_factory=list)
    diagnostic_summary: str | None = None
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str
    model_config = {"from_attributes": True}


class EventRead(BaseModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    payload_json: dict
    created_at: str
    model_config = {"from_attributes": True}
