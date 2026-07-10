from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    engine_type: str = Field(default="mock", pattern="^(mock|openai_compatible|codex_sdk)$")
    model_config_id: str | None = None


class RunRead(BaseModel):
    id: str
    challenge_id: str
    engine_type: str
    model_config_id: str | None
    status: str
    current_phase: str
    workspace_path: str
    codex_thread_id: str | None
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
