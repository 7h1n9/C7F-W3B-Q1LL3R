from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class SolveRun(UUIDTimestampMixin, Base):
    __tablename__ = "solve_runs"
    challenge_id: Mapped[str] = mapped_column(ForeignKey("challenges.id"), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(40), default="mock")
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    current_phase: Mapped[str] = mapped_column(String(80), default="CREATED")
    workspace_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    codex_thread_id: Mapped[str | None] = mapped_column(String(255))
    max_agent_steps: Mapped[int] = mapped_column(Integer, default=12)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=12)
    max_context_observations: Mapped[int] = mapped_column(Integer, default=8)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=300)
    agent_step_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))


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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))


class FlagCandidate(UUIDTimestampMixin, Base):
    __tablename__ = "flag_candidates"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    candidate: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    pattern_matched: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
