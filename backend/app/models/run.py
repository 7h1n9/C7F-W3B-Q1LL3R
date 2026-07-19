from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class SolveRun(UUIDTimestampMixin, Base):
    __tablename__ = "solve_runs"
    challenge_id: Mapped[str] = mapped_column(ForeignKey("challenges.id"), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(40), default="mock")
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    role_name: Mapped[str | None] = mapped_column(String(120))
    role_version: Mapped[str | None] = mapped_column(String(40))
    role_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    current_phase: Mapped[str] = mapped_column(String(80), default="CREATED")
    workspace_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    codex_thread_id: Mapped[str | None] = mapped_column(String(255))
    conversation_summary: Mapped[str | None] = mapped_column(Text)
    max_agent_steps: Mapped[int] = mapped_column(Integer, default=120)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=120)
    max_context_observations: Mapped[int] = mapped_column(Integer, default=8)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=900)
    max_total_runtime_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    agent_checkpoint_interval: Mapped[int] = mapped_column(Integer, default=30)
    context_revision: Mapped[int] = mapped_column(Integer, default=0)
    infrastructure_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    agent_step_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    run_total_agent_steps: Mapped[int] = mapped_column(Integer, default=0)
    run_total_logical_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    attempt_agent_steps: Mapped[int] = mapped_column(Integer, default=0)
    attempt_logical_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_segment_steps: Mapped[int] = mapped_column(Integer, default=0)
    current_attempt_number: Mapped[int] = mapped_column(Integer, default=0)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AgentTurn(UUIDTimestampMixin, Base):
    __tablename__ = "agent_turns"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    action_protocol: Mapped[str] = mapped_column(String(30), nullable=False, default="json_schema")
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    context_size_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    parse_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parse_error_code: Mapped[str | None] = mapped_column(String(100))
    response_excerpt_redacted: Mapped[str | None] = mapped_column(Text)
    action_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class RunAttempt(UUIDTimestampMixin, Base):
    __tablename__ = "run_attempts"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    engine_type: Mapped[str] = mapped_column(String(40), nullable=False)
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="RUNNING")
    error_code: Mapped[str | None] = mapped_column(String(100))
    agent_steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    attempt_agent_steps: Mapped[int] = mapped_column(Integer, default=0)
    attempt_logical_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    initial_agent_steps: Mapped[int] = mapped_column(Integer, default=0)
    initial_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    initial_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    initial_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The migration creates this column as NOT NULL.  Keep it in the ORM as
    # well so MySQL receives a value on the very first insert of an attempt.
    # Without this field a newly started run fails before the orchestrator can
    # transition it out of CREATED.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RunExecutionLease(UUIDTimestampMixin, Base):
    __tablename__ = "run_execution_leases"
    __table_args__ = (UniqueConstraint("run_id", name="uq_run_execution_lease_run"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id"), nullable=False)
    owner_instance_id: Mapped[str] = mapped_column(String(120), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RunUserInput(UUIDTimestampMixin, Base):
    __tablename__ = "run_user_inputs"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    input_type: Mapped[str] = mapped_column(String(40), default="SUPPLEMENT")
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("run_attempts.id"))


class RunEvent(UUIDTimestampMixin, Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ToolCall(UUIDTimestampMixin, Base):
    __tablename__ = "tool_calls"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="REQUESTED")
    runner_job_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logical_tool_call_id: Mapped[str | None] = mapped_column(String(120), index=True)
    parent_tool_call_id: Mapped[str | None] = mapped_column(String(120))
    execution_layer: Mapped[str] = mapped_column(String(40), default="gateway")


class Artifact(UUIDTimestampMixin, Base):
    __tablename__ = "artifacts"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), default="text/plain")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[str] = mapped_column(Text, default="")


class Observation(UUIDTimestampMixin, Base):
    __tablename__ = "observations"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    observation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    facts_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Hypothesis(UUIDTimestampMixin, Base):
    __tablename__ = "hypotheses"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="OPEN")
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class FlagCandidate(UUIDTimestampMixin, Base):
    __tablename__ = "flag_candidates"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    candidate: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    pattern_matched: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    review_state: Mapped[str] = mapped_column(String(20), default="OPEN")
