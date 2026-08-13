from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkerEngineSelection(BaseModel):
    engine_type: Literal["mock", "codex_sdk", "openai_compatible"]
    model_config_id: str | None = None

    @field_validator("engine_type", mode="before")
    @classmethod
    def normalize_engine_type(cls, value: object) -> object:
        aliases = {"codex": "codex_sdk", "codex-sdk": "codex_sdk", "openai-compatible": "openai_compatible"}
        normalized = str(value or "").strip().casefold()
        return aliases.get(normalized, normalized)

    @model_validator(mode="after")
    def validate_model_binding(self) -> "WorkerEngineSelection":
        if self.engine_type == "openai_compatible" and not self.model_config_id:
            raise ValueError("openai_compatible worker requires model_config_id")
        if self.engine_type != "openai_compatible" and self.model_config_id:
            raise ValueError(f"{self.engine_type} worker does not accept model_config_id")
        return self


class RunCreate(BaseModel):
    engine_type: str = Field(default="mock", pattern="^(mock|openai_compatible|codex_sdk)$")
    # New Runs expose only the two currently supported execution paths.
    # Existing database rows keep their persisted legacy mode and remain
    # readable through RunRead during the migration window.
    solver_mode: str = Field(default="muteki", pattern="^(single_agent|muteki)$")
    model_config_id: str | None = None
    reason_model_config_id: str | None = None
    worker_engines: list[WorkerEngineSelection] = Field(default_factory=list, max_length=8)
    max_agent_steps: int = Field(default=120, ge=1, le=300)
    max_tool_calls: int = Field(default=120, ge=0, le=300)
    max_context_observations: int = Field(default=8, ge=1, le=50)
    max_runtime_seconds: int = Field(default=900, ge=10, le=3600)
    max_total_runtime_seconds: int = Field(default=3600, ge=10, le=14400)
    selected_skill_ids: list[str] = Field(default_factory=list, max_length=30)
    disabled_skill_ids: list[str] = Field(default_factory=list, max_length=30)
    conversation_id: str | None = None


class RunBatchDelete(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=500)


class RunRead(BaseModel):
    id: str
    challenge_id: str
    challenge_name: str | None = None
    title: str | None = None
    challenge_type: str | None = None
    target_summary: str | None = None
    engine_type: str
    solver_mode: str = "multi_agent_v1"
    model_config_id: str | None
    reason_model_config_id: str | None = None
    reason_model_name: str | None = None
    worker_engines: list[dict] = Field(default_factory=list)
    model_name: str | None = None
    model_source: str | None = None
    model_config_required: bool = False
    model_config_applicable: bool = False
    bridge_ready: bool = False
    preflight_ready: bool = False
    recovery_checkpoint_json: dict = Field(default_factory=dict)
    workspace_revision: int = 0
    role_name: str | None
    role_version: str | None
    role_snapshot_json: dict
    hints_json: dict = Field(default_factory=dict)
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
