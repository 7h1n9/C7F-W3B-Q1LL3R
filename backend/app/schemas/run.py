from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.model_config import normalize_worker_engine_type


class WorkerEngineSelection(BaseModel):
    # ``cursor_cli`` was removed from this literal when the channel was retired
    # (see app.schemas.model_config.RETIRED_PROVIDER_TYPES).  Historical Runs keep
    # their stored engine_type -- the read model below types it as a plain string
    # on purpose, so old rows still render.
    engine_type: Literal["codex_cli", "openai_compatible", "claude_cli"]
    model_config_id: str | None = None

    @field_validator("engine_type", mode="before")
    @classmethod
    def normalize_engine_type(cls, value: object) -> object:
        # One alias table for the whole app; see app.schemas.model_config.
        return normalize_worker_engine_type(value) or value

    @model_validator(mode="after")
    def validate_model_binding(self) -> "WorkerEngineSelection":
        if not self.model_config_id:
            # Every engine gets its binary/model/credentials from a model
            # config, so an unbound Worker selection is unrunnable by definition.
            raise ValueError(f"{self.engine_type} worker requires model_config_id")
        return self


class RunCreate(BaseModel):
    engine_type: str = Field(default="openai_compatible")
    # New Runs expose only the currently supported execution paths.
    # Existing database rows keep their persisted legacy mode and remain
    # readable through RunRead during the migration window.
    solver_mode: str = Field(default="muteki", pattern="^(single_agent|muteki)$")

    @field_validator("engine_type", mode="before")
    @classmethod
    def normalize_engine_type(cls, value: object) -> object:
        normalized = normalize_worker_engine_type(value)
        if not normalized:
            raise ValueError(f"unsupported worker engine type: {value}")
        return normalized


    model_config_id: str | None = None
    reason_model_config_id: str | None = None
    worker_engines: list[WorkerEngineSelection] = Field(default_factory=list, max_length=8)
    #: Recall the curated corpus + distilled templates into this Run's context.
    #:
    #: ``None`` means "not specified", which is *not* the same as ``"off"``: the
    #: Run then falls back to the deployment default (``C7F_KNOWLEDGE_MODE``), so
    #: a caller that has no opinion does not silently pin the baseline arm of the
    #: A/B experiment.  A caller that *does* have an opinion (the arena queue, the
    #: control-experiment endpoint) must be able to state it explicitly, which is
    #: exactly what was missing: the arena path never wrote this key, so every
    #: batch solve ran with knowledge off while the corpus sat unused.
    knowledge_mode: Literal["off", "on"] | None = None
    max_agent_steps: int = Field(default=120, ge=1, le=300)
    max_tool_calls: int = Field(default=120, ge=0, le=300)
    max_context_observations: int = Field(default=8, ge=1, le=50)
    max_runtime_seconds: int = Field(default=480, ge=10, le=3600)
    max_total_runtime_seconds: int = Field(default=1800, ge=10, le=14400)
    selected_skill_ids: list[str] = Field(default_factory=list, max_length=30)
    disabled_skill_ids: list[str] = Field(default_factory=list, max_length=30)
    conversation_id: str | None = None


class RunControlExperimentCreate(BaseModel):
    factor: Literal["knowledge_mode"]
    counterfactual_value: Literal["off", "on"]
    start: bool = False


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
