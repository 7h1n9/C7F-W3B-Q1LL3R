from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


SCRIPT_RECORD_STATUSES = frozenset(
    {
        "CREATED",
        "VALIDATING",
        "VALIDATED",
        "RUNNING",
        "COMPLETED",
        "PARTIAL",
        "FAILED",
        "BLOCKED_DEPLOYMENT",
        "CANCELLED",
    }
)


class SolveRun(UUIDTimestampMixin, Base):
    __tablename__ = "solve_runs"
    challenge_id: Mapped[str] = mapped_column(ForeignKey("challenges.id"), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(40), default="mock")
    # New Runs default to the structured multi-agent controller.  Existing
    # rows keep their persisted mode, and single_agent remains supported as an
    # explicit compatibility mode.
    solver_mode: Mapped[str] = mapped_column(
        String(30), default="multi_agent_v1", server_default="multi_agent_v1", nullable=False
    )
    controller_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    role_name: Mapped[str | None] = mapped_column(String(120))
    role_version: Mapped[str | None] = mapped_column(String(40))
    role_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    hints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    current_phase: Mapped[str] = mapped_column(String(80), default="INTAKE", server_default="INTAKE")
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
    terminal_generation: Mapped[int] = mapped_column(Integer, default=0)
    terminal_event_sequence: Mapped[int | None] = mapped_column(Integer)
    thread_invalidated: Mapped[bool] = mapped_column(Boolean, default=False)
    post_terminal_events_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    fresh_reproduction_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Compaction is a first-class checkpoint in the run lifecycle.  These
    # counters are denormalized for cheap trigger checks; the archive and
    # snapshot tables remain the source of truth for recovery.
    last_compaction_effective_tool_count: Mapped[int] = mapped_column(Integer, default=0)
    compaction_generation: Mapped[int] = mapped_column(Integer, default=0)
    compaction_status: Mapped[str] = mapped_column(String(30), default="IDLE")
    compaction_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compaction_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compacted_event_count: Mapped[int] = mapped_column(Integer, default=0)
    compacted_observation_count: Mapped[int] = mapped_column(Integer, default=0)
    compacted_artifact_count: Mapped[int] = mapped_column(Integer, default=0)
    compacted_trace_count: Mapped[int] = mapped_column(Integer, default=0)
    last_compaction_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    reserved_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tool_calls_by_turn_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Required fallback actions have an independent reservation pool.  A
    # general model/tool burst must never consume the four calls needed to
    # materialize and execute a bounded extraction script.
    reserved_required_action_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_required_action_calls_by_type_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    required_action_calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assistance_level: Mapped[str] = mapped_column(String(30), default="AUTONOMOUS")
    assistance_sources_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    recovery_checkpoint_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    workspace_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    workspace_negative_cache_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_compacted_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_compacted_tool_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_compacted_observation_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_compacted_artifact_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_compacted_trace_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    infrastructure_error_streak: Mapped[int] = mapped_column(Integer, default=0)
    infrastructure_state: Mapped[str] = mapped_column(String(40), default="HEALTHY")
    infrastructure_last_error_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    active_turn_id: Mapped[str | None] = mapped_column(String(36), index=True)
    terminal_cleanup_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    terminal_cleanup_manifest_id: Mapped[str | None] = mapped_column(String(36), index=True)
    terminal_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_evidence_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cleanup_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AgentTurn(UUIDTimestampMixin, Base):
    __tablename__ = "agent_turns"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    agent_role: Mapped[str | None] = mapped_column(String(30), index=True)
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
    turn_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    turn_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunAttempt(UUIDTimestampMixin, Base):
    __tablename__ = "run_attempts"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    engine_type: Mapped[str] = mapped_column(String(40), nullable=False)
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    hints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="RUNNING")
    runtime_build_manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tool_manifest_status: Mapped[str] = mapped_column(String(40), default="UNSET", nullable=False)
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


class RunContinuation(UUIDTimestampMixin, Base):
    """Durable request to re-enter the run controller after a boundary."""

    __tablename__ = "run_continuations"
    __table_args__ = (UniqueConstraint("run_id", "dedupe_key", name="uq_run_continuation_dedupe"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("run_attempts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    owner_instance_id: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class AttemptToolManifest(UUIDTimestampMixin, Base):
    """Effective tool catalog captured for one Attempt.

    Historical role snapshots remain immutable; this records what was
    actually available after policy, Runner and MCP intersection.
    """

    __tablename__ = "attempt_tool_manifests"
    __table_args__ = (UniqueConstraint("attempt_id", name="uq_attempt_tool_manifest_attempt"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id"), nullable=False, index=True)
    role_snapshot_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    challenge_allowed_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    backend_registry_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    runner_capability_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    mcp_advertised_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    execution_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="controller_tool_loop")
    mcp_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effective_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    missing_expected_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    schema_hashes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    network_enforcement_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tool_capabilities_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
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


class ToolInvocationTicket(UUIDTimestampMixin, Base):
    __tablename__ = "tool_invocation_tickets"
    __table_args__ = (UniqueConstraint("ticket_hash", name="uq_tool_invocation_ticket_hash"),)

    ticket_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id"), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255))
    model_turn_id: Mapped[str | None] = mapped_column(String(255))
    lease_id: Mapped[str] = mapped_column(ForeignKey("run_execution_leases.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # Monotonic database-side ordering key.  ``sequence`` is retained for
    # backwards compatibility with the SSE contract and old dumps.
    event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True, autoincrement=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_size: Mapped[int] = mapped_column(Integer, default=0)
    payload_digest: Mapped[str] = mapped_column(String(64), default="")


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
    counts_toward_budget: Mapped[bool] = mapped_column(Boolean, default=True)
    logical_kind: Mapped[str] = mapped_column(String(40), default="TOOL")
    provider_tool_name: Mapped[str | None] = mapped_column(String(120))
    effective_tool_name: Mapped[str | None] = mapped_column(String(120))
    turn_id: Mapped[str | None] = mapped_column(String(36), index=True)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    approved_action_id: Mapped[str | None] = mapped_column(ForeignKey("approved_actions.id"), index=True)
    agent_role: Mapped[str | None] = mapped_column(String(30), index=True)
    task_lease_token: Mapped[str | None] = mapped_column(String(120))


class LogicalToolCall(UUIDTimestampMixin, Base):
    __tablename__ = "logical_tool_calls"
    __table_args__ = (UniqueConstraint("run_id", "id", name="uq_logical_tool_call_run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("run_attempts.id"), index=True)
    engine_type: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="REQUESTED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_observation_id: Mapped[str | None] = mapped_column(ForeignKey("observations.id"))
    counts_toward_budget: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    logical_kind: Mapped[str] = mapped_column(String(40), default="TOOL")
    provider_tool_name: Mapped[str | None] = mapped_column(String(120))
    effective_tool_name: Mapped[str | None] = mapped_column(String(120))
    turn_id: Mapped[str | None] = mapped_column(String(36), index=True)
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ToolExecutionTrace(UUIDTimestampMixin, Base):
    __tablename__ = "tool_execution_traces"
    __table_args__ = (UniqueConstraint(
        "logical_tool_call_id", "execution_layer", "event_type", "external_id", "payload_digest",
        name="uq_tool_trace_identity",
    ),)
    logical_tool_call_id: Mapped[str] = mapped_column(ForeignKey("logical_tool_calls.id"), nullable=False, index=True)
    execution_layer: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


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
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    retention_class: Mapped[str] = mapped_column(String(30), default="PROTECTED", nullable=False)
    temporary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    terminal_referenced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScriptRecord(UUIDTimestampMixin, Base):
    """Provenance for generated exploit scripts, separate from execution output."""

    __tablename__ = "scripts"
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(30), default="MODEL_GENERATED")
    assistance_level: Mapped[str] = mapped_column(String(30), default="AUTONOMOUS")
    assumption_provenance_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    design_card_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # ``path`` is retained for compatibility with the original provenance
    # table; script_path is the explicit lifecycle field used by the
    # controller and Runner.
    script_path: Mapped[str | None] = mapped_column(String(1024))
    agent_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    objective: Mapped[str] = mapped_column(Text, default="")
    network_mode: Mapped[str] = mapped_column(String(40), default="none")
    allowed_hosts_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    max_requests: Mapped[int] = mapped_column(Integer, default=0)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=60)
    validation_error: Mapped[str | None] = mapped_column(Text)
    execution_error: Mapped[str | None] = mapped_column(Text)
    result_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    checkpoint_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    status: Mapped[str] = mapped_column(String(20), default="CREATED", nullable=False)


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
    first_seen_source_type: Mapped[str | None] = mapped_column(String(30))
    first_seen_source_id: Mapped[str | None] = mapped_column(String(36))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    source_agent_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    source_assistance_level: Mapped[str] = mapped_column(String(30), default="AUTONOMOUS", nullable=False)


class FlagProvenance(UUIDTimestampMixin, Base):
    __tablename__ = "flag_provenance"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_flag_provenance_candidate"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("flag_candidates.id"), nullable=False)
    first_seen_source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    first_seen_source_id: Mapped[str | None] = mapped_column(String(36))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    source_tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    source_agent_task_id: Mapped[str | None] = mapped_column(String(36))
    source_assistance_level: Mapped[str] = mapped_column(String(30), nullable=False, default="AUTONOMOUS")
    verification_source_type: Mapped[str | None] = mapped_column(String(30))
    verification_source_id: Mapped[str | None] = mapped_column(String(36))
    source_is_autonomous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CleanupManifest(UUIDTimestampMixin, Base):
    __tablename__ = "cleanup_manifests"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_cleanup_manifest_idempotency"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    agent_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cleanup_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PLANNED")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archive_path: Mapped[str | None] = mapped_column(String(1024))
    archive_sha256: Mapped[str | None] = mapped_column(String(64))
    retention_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_paths_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    preserved_paths_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    debug_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ToolBatchSummary(UUIDTimestampMixin, Base):
    __tablename__ = "tool_batch_summaries"
    __table_args__ = (UniqueConstraint("run_id", "logical_tool_call_id", name="uq_tool_batch_run_logical"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    agent_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    logical_tool_call_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    subrequest_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    result_artifact_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="COMPLETED")


class ToolRequestFingerprint(UUIDTimestampMixin, Base):
    __tablename__ = "tool_request_fingerprints"
    __table_args__ = (UniqueConstraint("run_id", "fingerprint", name="uq_tool_request_run_fingerprint"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_arguments_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    stage: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logical_tool_call_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SCHEDULED")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


class WebResearchRecord(UUIDTimestampMixin, Base):
    __tablename__ = "web_research_records"

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    agent_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    query_type: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(30), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    answer_leak_risk: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="EPHEMERAL")
    source_urls_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    used_in_fact_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    runtime_path: Mapped[str | None] = mapped_column(String(1024))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunCompactionCheckpoint(UUIDTimestampMixin, Base):
    """Durable state machine row for one review/apply/archive operation."""

    __tablename__ = "run_compaction_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "generation", name="uq_compaction_run_generation"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="COMPACTION_REVIEW")
    reason: Mapped[str] = mapped_column(Text, default="")
    decision_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    archive_path: Mapped[str | None] = mapped_column(String(1024))
    archive_manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deleted_row_counts_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class EvidenceSnapshot(UUIDTimestampMixin, Base):
    __tablename__ = "run_evidence_snapshots"

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("run_compaction_checkpoints.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class CompactionLease(UUIDTimestampMixin, Base):
    """Cross-process lease for asynchronous compaction workers."""

    __tablename__ = "compaction_leases"
    __table_args__ = (UniqueConstraint("run_id", name="uq_compaction_lease_run"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
