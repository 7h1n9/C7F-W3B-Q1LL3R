"""Public contracts for the structured multi-agent controller."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRole(StrEnum):
    PLANNER = "PLANNER"
    RECON = "RECON"
    ANALYSIS = "ANALYSIS"
    EXPLOIT = "EXPLOIT"
    VERIFY = "VERIFY"


class AgentTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NEED_REPLAN = "NEED_REPLAN"
    CANCELLED = "CANCELLED"


class TaskBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_logical_calls: int = Field(default=1, ge=0, le=1000)
    max_internal_requests: int = Field(default=8, ge=0, le=10000)
    max_runtime_seconds: int = Field(default=120, ge=1, le=86400)


class AgentTaskContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    agent_role: AgentRole
    objective: str = Field(min_length=1, max_length=4000)
    known_fact_ids: list[str] = Field(default_factory=list, max_length=500)
    active_hypothesis_ids: list[str] = Field(default_factory=list, max_length=500)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    success_condition: str = Field(default="", max_length=2000)
    stop_conditions: list[str] = Field(default_factory=list, max_length=50)
    evidence_snapshot_id: str | None = None
    created_by_task_id: str | None = None
    lease_id: str | None = None
    lease_expires_at: str | None = None
    optimistic_version: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=120, ge=1, le=86400)
    cancel_requested: bool = False
    retry_count: int = Field(default=0, ge=0, le=100)
    input_snapshot_version: int = Field(default=0, ge=0)
    result_schema_version: str = "v1"


class FailureClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1, max_length=255)
    classification: str = Field(min_length=1, max_length=80)
    retryable: bool = False
    reason: str = Field(default="", max_length=4000)
    next_allowed_condition: str = Field(default="", max_length=2000)


class AgentTaskResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)
    status: AgentTaskStatus
    new_facts: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    updated_hypotheses: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    accepted_solution_steps: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rejected_paths: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    failure_classification: FailureClassification | None = None
    proposed_next_action: dict[str, Any] = Field(default_factory=dict)
    handoff_summary: str = Field(default="", max_length=4000)
    schema_version: str = "v1"
    # Transport-only field used by the API; it is never persisted in the
    # result schema and is not part of the agent's durable output.
    lease_token: str | None = None

    @model_validator(mode="after")
    def task_result_has_failure_for_failure_status(self) -> "AgentTaskResultContract":
        if self.status in {AgentTaskStatus.FAILED, AgentTaskStatus.BLOCKED} and self.failure_classification is None:
            raise ValueError("failed or blocked tasks must include failure_classification")
        return self


class AgentRolePolicyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AgentRole
    system_prompt: str = ""
    readable_memory_types: list[str] = Field(default_factory=list)
    allowed_tool_types: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_outputs: list[str] = Field(default_factory=list)
    forbidden_operations: list[str] = Field(default_factory=list)
    max_logical_calls: int = Field(default=1, ge=0)
    max_internal_requests: int = Field(default=8, ge=0)
    max_runtime_seconds: int = Field(default=120, ge=1)
    default_timeout_seconds: int = Field(default=120, ge=1)
    max_retries: int = Field(default=1, ge=0)
    can_create_candidate_fact: bool = False
    can_verify_fact: bool = False
    can_submit_flag: bool = False
    can_change_run_status: bool = False


class PlannerProposalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    current_stage: str = Field(min_length=1, max_length=40)
    next_agent: AgentRole
    objective: str = Field(min_length=1, max_length=4000)
    input_fact_ids: list[str] = Field(default_factory=list, max_length=500)
    required_capabilities: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    success_condition: str = Field(min_length=1, max_length=2000)
    stop_conditions: list[str] = Field(default_factory=list, max_length=50)
    fallback: str = Field(default="RETURN_TO_ANALYSIS", max_length=80)


class AnalysisDecision(StrEnum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
    RETURN_TO_RECON = "RETURN_TO_RECON"
    SWITCH_HYPOTHESIS = "SWITCH_HYPOTHESIS"
    ABANDON_PATH = "ABANDON_PATH"


class AnalysisReviewContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=80)
    decision: AnalysisDecision
    confidence: int = Field(default=0, ge=0, le=100)
    question_being_tested: str = ""
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    independent_variable: str | None = None
    required_controls: dict[str, Any] = Field(default_factory=dict)
    expected_true_signal: dict[str, Any] = Field(default_factory=dict)
    expected_false_signal: dict[str, Any] = Field(default_factory=dict)
    recommended_tool: str | None = None
    reason: str = ""
    audit_reason: str = ""


class PromotionStatus(StrEnum):
    VERIFIED = "VERIFIED"
    CANDIDATE = "CANDIDATE"
    DUPLICATE = "DUPLICATE"
    SUPERSEDED = "SUPERSEDED"
    NO_VALUE = "NO_VALUE"


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PromotionStatus
    reason: str = ""
    promoted_ids: list[str] = Field(default_factory=list)


class EvidenceLedgerContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: str = Field(min_length=1, max_length=80)
    run_id: str
    evidence_type: str
    artifact_id: str | None = None
    tool_call_id: str | None = None
    agent_task_id: str | None = None
    summary: str = ""
    sha256: str = ""
    status: str = "VERIFIED"
    retention_class: str = "PROTECTED"
    source_chain: list[str] = Field(default_factory=list)


class SolutionChainNodeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=80)
    run_id: str
    stage: str = Field(min_length=1, max_length=40)
    objective: str = Field(min_length=1, max_length=4000)
    input_fact_ids: list[str] = Field(default_factory=list)
    agent_task_id: str
    logical_tool_call_id: str | None = None
    result_fact_ids: list[str] = Field(default_factory=list)
    capability_added: str = Field(min_length=1, max_length=255)
    evidence_ids: list[str] = Field(min_length=1)


class CandidateVerificationContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(min_length=1, max_length=1000)
    verify_task_id: str
    source_artifact_id: str
    producing_tool_call_id: str
    evidence_ids: list[str] = Field(min_length=1)
    pattern_matched: bool
    fresh_reproduction: bool
    assistance_level: str = "AUTONOMOUS"
