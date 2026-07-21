from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class SolverState(UUIDTimestampMixin, Base):
    __tablename__ = "solver_states"
    __table_args__ = (UniqueConstraint("run_id", name="uq_solver_state_run"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    current_phase: Mapped[str] = mapped_column(String(80), nullable=False, default="INTAKE")
    confirmed_facts_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    rejected_paths_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    active_hypotheses_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    action_fingerprints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    active_skill_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skill_recommendations_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    run_plan_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    capability_ledger_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    read_files_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    read_ranges_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    content_hashes_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_decision_card_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_experiment_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attack_chain_plan_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    experiment_dimensions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_result_classification: Mapped[str | None] = mapped_column(String(40))
    finish_rejection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    force_plan_action: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_progress_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    investigation_no_progress_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_action_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    control_rejection_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    schema_error_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    degraded_action_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
