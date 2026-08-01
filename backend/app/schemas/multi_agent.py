"""Public contracts for the structured multi-agent controller."""

from enum import StrEnum
from typing import Annotated, Any, Literal

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
    PARTIAL = "PARTIAL"
    INTERRUPTED = "INTERRUPTED"


class AgentTaskKind(StrEnum):
    PLANNING = "PLANNING"
    PLAN_REVIEW = "PLAN_REVIEW"
    RECON = "RECON"
    EXPLOIT = "EXPLOIT"
    RESULT_REVIEW = "RESULT_REVIEW"
    VERIFY = "VERIFY"


class TaskBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_logical_calls: int = Field(default=1, ge=0, le=1000)
    max_internal_requests: int = Field(default=8, ge=0, le=10000)
    max_runtime_seconds: int = Field(default=300, ge=1, le=86400)


class RoleToolAction(BaseModel):
    """The only executable action a production role may propose per turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["tool"]
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str = Field(min_length=1, max_length=2000)
    expected_signal: dict[str, Any] = Field(default_factory=dict)
    stop_if: list[str] = Field(default_factory=list, max_length=50)


class RoleFinishAction(BaseModel):
    """A role can finish only with the durable task result contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["finish"]
    result: "AgentTaskResultContract"


RoleAction = Annotated[RoleToolAction | RoleFinishAction, Field(discriminator="type")]


class ScriptProposalContract(BaseModel):
    """Structured proposal consumed by the Script controller.

    The controller, not the model, turns this proposal into the durable
    CREATE/VALIDATE/EXECUTE chain.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["CREATE_BOUNDED_SCRIPT"]
    script_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", min_length=1, max_length=120)
    language: Literal["python", "node", "bash"]
    objective: str = Field(min_length=1, max_length=2000)
    network_mode: Literal["none", "target_allowlist"]
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)
    max_requests: int = Field(default=1, ge=1, le=1000)
    max_runtime_seconds: int = Field(default=60, ge=1, le=600)
    checkpoint: str = Field(min_length=1, max_length=255)
    resume: str = Field(min_length=1, max_length=1000)
    script_content: str = Field(min_length=1, max_length=200000)

    @model_validator(mode="after")
    def network_requires_hosts(self) -> "ScriptProposalContract":
        if self.network_mode == "target_allowlist" and not self.allowed_hosts:
            raise ValueError("target_allowlist requires allowed_hosts")
        return self


class AgentTaskContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    agent_role: AgentRole
    task_kind: AgentTaskKind = AgentTaskKind.RECON
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
    context: dict[str, Any] = Field(default_factory=dict)


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


class ProductionResultContext(BaseModel):
    """Durable, cross-session input for a RESULT_REVIEW turn.

    This is deliberately a transport snapshot rather than a collection of
    SQLAlchemy objects.  The controller builds it only after the producing
    ToolCall, Artifact, Observation, EvidenceLedger and task result have been
    committed, then the Analysis turn can safely use it in a new transaction.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    attempt_id: str
    proposal_id: str
    plan_review_id: str
    approved_action_id: str
    agent_task_id: str
    task_status: str
    task_result: dict[str, Any]
    proposal: dict[str, Any]
    plan_review: dict[str, Any]
    approved_action: dict[str, Any]
    production_task: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    evidence_ids: list[str]
    candidate_facts: list[dict[str, Any]]
    current_verified_fact_ids: list[str]
    current_capabilities: dict[str, Any]
    current_phase: str
    success_condition: str = ""


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
    decision_question: str = Field(default="", max_length=2000)
    next_agent: AgentRole
    objective: str = Field(min_length=1, max_length=4000)
    input_fact_ids: list[str] = Field(default_factory=list, max_length=500)
    input_evidence_ids: list[str] = Field(default_factory=list, max_length=500)
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
    task_kind: Literal["PLAN_REVIEW", "RESULT_REVIEW"] = "PLAN_REVIEW"
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
    approved_arguments: dict[str, Any] = Field(default_factory=dict)
    approved_fact_indexes: list[int] = Field(default_factory=list, max_length=200)
    approved_evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    approved_hypothesis_updates: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    capabilities_added: list[str] = Field(default_factory=list, max_length=100)
    solution_step_accepted: bool = False
    next_phase: str = Field(default="HYPOTHESIS", max_length=80)


class ApprovedActionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_action_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    proposal_id: str = Field(min_length=1, max_length=80)
    analysis_review_id: str = Field(min_length=1, max_length=80)
    agent_role: AgentRole
    tool_name: str = Field(min_length=1, max_length=100)
    argument_constraints: dict[str, Any] = Field(default_factory=dict)
    max_logical_calls: int = Field(default=1, ge=1, le=1000)
    expires_at: str
    status: Literal["PENDING_COMPILE", "COMPILED", "ACTIVE", "CONSUMED", "REJECTED", "EXPIRED", "REVOKED"] = "ACTIVE"


class CompiledApprovedAction(BaseModel):
    """Controller-owned, schema-ready execution capability.

    This is deliberately separate from ``AnalysisReview.approved_arguments``:
    the latter is semantic approval input, while this object is the only
    allowed source for Runner arguments.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    arguments_digest: str
    tool_schema_hash: str
    compiler_name: str
    compiler_version: str
    source_review_id: str
    source_proposal_id: str


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
