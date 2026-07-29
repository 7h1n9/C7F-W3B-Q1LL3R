"""Durable records for the first multi-agent solving loop.

These tables intentionally store compact, structured summaries.  Agent
transcripts are not a source of truth and are therefore not persisted here.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


def _now() -> datetime:
    return datetime.now(UTC)


class AgentRolePolicy(UUIDTimestampMixin, Base):
    __tablename__ = "agent_role_policies"
    __table_args__ = (UniqueConstraint("role", name="uq_agent_role_policy_role"),)

    role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    readable_memory_types_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allowed_tool_types_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allowed_outputs_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    forbidden_operations_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    max_logical_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_internal_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    default_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    can_create_candidate_fact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_verify_fact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_submit_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_change_run_status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False, default="v1")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AgentTask(UUIDTimestampMixin, Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (UniqueConstraint("run_id", "id", name="uq_agent_task_run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    agent_role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    task_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="RECON", index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    known_fact_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    active_hypothesis_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    budget_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    success_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stop_conditions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_by_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING", index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_token: Mapped[str | None] = mapped_column(String(120), unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    optimistic_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    total_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idle_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_schema_version: Mapped[str] = mapped_column(String(30), nullable=False, default="v1")
    runtime_path: Mapped[str | None] = mapped_column(String(1024))
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ApprovedAction(UUIDTimestampMixin, Base):
    """Controller-issued capability to execute one approved tool contract."""

    __tablename__ = "approved_actions"
    __table_args__ = (UniqueConstraint("run_id", "approved_action_id", name="uq_approved_action_run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    approved_action_id: Mapped[str] = mapped_column(String(80), nullable=False)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("planner_proposals.id"), nullable=False)
    analysis_review_id: Mapped[str] = mapped_column(ForeignKey("analysis_reviews.id"), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(30), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    argument_constraints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    max_logical_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_logical_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentTaskResult(UUIDTimestampMixin, Base):
    __tablename__ = "agent_task_results"
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    new_facts_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_hypotheses_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    accepted_solution_steps_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rejected_paths_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    failure_classification_json: Mapped[dict | None] = mapped_column(JSON)
    proposed_next_action_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    handoff_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False, default="v1")


class PlannerProposal(UUIDTimestampMixin, Base):
    __tablename__ = "planner_proposals"
    __table_args__ = (UniqueConstraint("run_id", "proposal_id", name="uq_planner_proposal_run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(String(80), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    decision_question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_agent: Mapped[str] = mapped_column(String(30), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input_fact_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    required_capabilities_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    budget_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    success_condition: Mapped[str] = mapped_column(Text, nullable=False)
    stop_conditions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fallback: Mapped[str] = mapped_column(String(80), nullable=False, default="RETURN_TO_ANALYSIS")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PROPOSED", index=True)
    created_by_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))


class AnalysisReview(UUIDTimestampMixin, Base):
    __tablename__ = "analysis_reviews"
    __table_args__ = (UniqueConstraint("proposal_id", "task_kind", name="uq_analysis_review_kind"),)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("planner_proposals.id"), nullable=False)
    task_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="PLAN_REVIEW")
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_being_tested: Mapped[str] = mapped_column(Text, nullable=False, default="")
    supporting_evidence_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    independent_variable: Mapped[str | None] = mapped_column(String(255))
    required_controls_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expected_true_signal_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expected_false_signal_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    recommended_tool: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    audit_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approved_arguments_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    approved_fact_indexes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    approved_evidence_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    approved_hypothesis_updates_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    capabilities_added_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    solution_step_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_phase: Mapped[str] = mapped_column(String(80), nullable=False, default="HYPOTHESIS")


class VerifiedFact(UUIDTimestampMixin, Base):
    __tablename__ = "verified_facts"
    __table_args__ = (UniqueConstraint("run_id", "fact_key", name="uq_verified_fact_run_key"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    fact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    value_json: Mapped[dict | list | str | int | bool | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    promotion_status: Mapped[str] = mapped_column(String(20), nullable=False, default="CANDIDATE")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class EvidenceLedger(UUIDTimestampMixin, Base):
    __tablename__ = "evidence_ledger"
    evidence_type: Mapped[str] = mapped_column(String(80), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="VERIFIED")
    retention_class: Mapped[str] = mapped_column(String(30), nullable=False, default="PROTECTED")
    source_chain: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class SolutionChainNode(UUIDTimestampMixin, Base):
    __tablename__ = "solution_chain_nodes"
    __table_args__ = (UniqueConstraint("run_id", "node_id", name="uq_solution_node_run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(80), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input_fact_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    agent_task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), nullable=False)
    logical_tool_call_id: Mapped[str | None] = mapped_column(String(120))
    result_fact_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    capability_added: Mapped[str | None] = mapped_column(String(255))
    evidence_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CANDIDATE")


class FailureSignature(UUIDTimestampMixin, Base):
    __tablename__ = "failure_signatures"
    __table_args__ = (UniqueConstraint("run_id", "fingerprint", name="uq_failure_signature_run_fp"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    classification: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_allowed_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")


class MemorySnapshot(UUIDTimestampMixin, Base):
    __tablename__ = "memory_snapshots"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    working_memory_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    verified_fact_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    hypothesis_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
