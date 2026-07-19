from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    engine_type: str = Field(default="mock", pattern="^(mock|openai_compatible|codex_sdk)$")
    model_config_id: str | None = None
    max_agent_steps: int = Field(default=120, ge=1, le=300)
    max_tool_calls: int = Field(default=120, ge=0, le=300)
    max_context_observations: int = Field(default=8, ge=1, le=50)
    max_runtime_seconds: int = Field(default=900, ge=10, le=3600)
    max_total_runtime_seconds: int = Field(default=3600, ge=10, le=14400)
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
    max_total_runtime_seconds: int = 3600
    agent_checkpoint_interval: int = 30
    context_revision: int = 0
    infrastructure_retry_count: int = 0
    agent_step_count: int
    tool_call_count: int
    run_total_agent_steps: int = 0
    run_total_logical_tool_calls: int = 0
    attempt_agent_steps: int = 0
    attempt_logical_tool_calls: int = 0
    checkpoint_segment_steps: int = 0
    current_attempt_number: int = 0
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
