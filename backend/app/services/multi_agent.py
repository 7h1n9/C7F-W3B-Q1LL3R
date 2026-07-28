"""Controller-owned multi-agent primitives.

The service is deliberately boring: agents propose structured values and this
module alone leases tasks, promotes memory, and changes run state.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.multi_agent import (
    AgentRolePolicy,
    AgentTask,
    AgentTaskResult,
    EvidenceLedger,
    FailureSignature,
    MemorySnapshot,
    SolutionChainNode,
    VerifiedFact,
)
from app.models.run import Artifact, FlagCandidate, FlagProvenance, SolveRun, ToolCall
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.multi_agent import (
    AgentRole,
    AgentRolePolicyContract,
    AgentTaskContract,
    AgentTaskResultContract,
    AgentTaskStatus,
    AnalysisDecision,
    AnalysisReviewContract,
    EvidenceLedgerContract,
    PlannerProposalContract,
    PromotionDecision,
    PromotionStatus,
    SolutionChainNodeContract,
)

DEFAULT_POLICIES: tuple[AgentRolePolicyContract, ...] = (
    AgentRolePolicyContract(
        role=AgentRole.PLANNER,
        system_prompt="Plan one bounded next stage from verified facts.",
        readable_memory_types=["WORKING", "VERIFIED_FACT", "HYPOTHESIS", "EVIDENCE", "FAILURE"],
        allowed_tool_types=[], allowed_tools=[], allowed_outputs=["PLANNER_PROPOSAL"],
        forbidden_operations=["RUNNER_CALL", "FLAG_SUBMIT", "RUN_STATUS_CHANGE"],
    ),
    AgentRolePolicyContract(
        role=AgentRole.RECON,
        system_prompt="Discover bounded target surface and normalize observations into facts.",
        readable_memory_types=["WORKING", "VERIFIED_FACT", "EVIDENCE"],
        allowed_tool_types=["RECON"],
        allowed_tools=["http_request", "content_discovery", "nmap_service_probe", "whatweb_fingerprint"],
        allowed_outputs=["RECON_FACT", "EVIDENCE"],
        forbidden_operations=["EXPLOIT", "FLAG_SUBMIT", "RUN_STATUS_CHANGE"],
        can_create_candidate_fact=True,
    ),
    AgentRolePolicyContract(
        role=AgentRole.ANALYSIS,
        system_prompt="Review proposals, control variables, and evidence quality.",
        readable_memory_types=["WORKING", "VERIFIED_FACT", "HYPOTHESIS", "EVIDENCE", "FAILURE"],
        allowed_tool_types=["ANALYSIS"],
        allowed_tools=["oracle_probe_matrix", "http_compare", "evidence_query"],
        allowed_outputs=["ANALYSIS_REVIEW", "HYPOTHESIS", "EVIDENCE"],
        forbidden_operations=["RUNNER_CALL", "FLAG_SUBMIT", "RUN_STATUS_CHANGE"],
        can_create_candidate_fact=True,
    ),
    AgentRolePolicyContract(
        role=AgentRole.EXPLOIT,
        system_prompt="Execute only the approved bounded task and return artifacts and evidence.",
        readable_memory_types=["WORKING", "VERIFIED_FACT", "HYPOTHESIS", "EVIDENCE"],
        allowed_tool_types=["EXPLOIT"],
        allowed_tools=["sqlmap_run", "boolean_config_extract", "script_run", "http_request"],
        allowed_outputs=["ARTIFACT", "EVIDENCE", "FLAG_CANDIDATE"],
        forbidden_operations=["REPLAN", "BUDGET_BYPASS", "RUN_STATUS_CHANGE", "FLAG_VERIFY"],
    ),
    AgentRolePolicyContract(
        role=AgentRole.VERIFY,
        system_prompt="Independently verify a structured candidate with fresh reproduction.",
        readable_memory_types=["WORKING", "VERIFIED_FACT", "EVIDENCE"],
        allowed_tool_types=["VERIFY"], allowed_tools=["http_request", "script_run"],
        allowed_outputs=["VERIFICATION_RESULT", "FRESH_REPRODUCTION"],
        forbidden_operations=["EXPLOIT", "RUN_STATUS_CHANGE"], can_verify_fact=True,
    ),
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class AgentPermissionController:
    """Centralized role and tool checks used before task/tool execution."""

    async def policy(self, session: AsyncSession, role: AgentRole | str) -> AgentRolePolicy:
        role_value = AgentRole(role).value
        policy = await session.scalar(select(AgentRolePolicy).where(AgentRolePolicy.role == role_value, AgentRolePolicy.enabled.is_(True)))
        if policy is None:
            raise DomainError("AGENT_ROLE_NOT_CONFIGURED", "The agent role has no enabled policy.", {"role": role_value})
        return policy

    @staticmethod
    def check_tool(policy: AgentRolePolicy, tool_name: str) -> None:
        if tool_name not in (policy.allowed_tools_json or []):
            raise DomainError("AGENT_TOOL_NOT_ALLOWED", "The role is not allowed to use this tool.", {"role": policy.role, "tool": tool_name})

    @staticmethod
    def check_operation(policy: AgentRolePolicy, operation: str) -> None:
        forbidden = set(policy.forbidden_operations_json or [])
        if operation in forbidden:
            raise DomainError("AGENT_OPERATION_FORBIDDEN", "The role cannot perform this operation.", {"role": policy.role, "operation": operation})


class PromotionGate:
    """Promote only new, evidence-backed structured output."""

    async def promote_result(self, session: AsyncSession, task: AgentTask, result: AgentTaskResultContract) -> PromotionDecision:
        if not result.new_facts and not result.evidence_ids and not result.accepted_solution_steps:
            return PromotionDecision(status=PromotionStatus.NO_VALUE, reason="No new fact, evidence, or solution capability.")
        if (result.new_facts or result.accepted_solution_steps) and not result.evidence_ids:
            return PromotionDecision(status=PromotionStatus.NO_VALUE, reason="Facts and solution steps require protected evidence.")
        if result.evidence_ids:
            evidence = list((await session.scalars(select(EvidenceLedger).where(EvidenceLedger.id.in_(result.evidence_ids), EvidenceLedger.run_id == task.run_id))).all())
            if len(evidence) != len(set(result.evidence_ids)):
                raise DomainError("EVIDENCE_NOT_FOUND", "Every promoted output must reference evidence in this run.")
        promoted: list[str] = []
        duplicate = False
        for raw in result.new_facts:
            key = str(raw.get("fact_key") or raw.get("key") or raw.get("type") or "").strip()
            if not key:
                continue
            existing = await session.scalar(select(VerifiedFact).where(VerifiedFact.run_id == task.run_id, VerifiedFact.fact_key == key))
            if existing:
                duplicate = True
                continue
            fact = VerifiedFact(
                run_id=task.run_id, fact_key=key, fact_type=str(raw.get("fact_type") or raw.get("type") or "GENERAL"),
                value_json=raw.get("value"), confidence=max(0, min(100, int(raw.get("confidence", 0)))),
                evidence_ids_json=list(result.evidence_ids), source_task_id=task.id, promotion_status="CANDIDATE",
            )
            session.add(fact)
            await session.flush()
            promoted.append(fact.id)
        if promoted:
            return PromotionDecision(status=PromotionStatus.CANDIDATE, reason="New evidence-backed facts are awaiting verification.", promoted_ids=promoted)
        if duplicate:
            return PromotionDecision(status=PromotionStatus.DUPLICATE, reason="The structured facts already exist in memory.")
        return PromotionDecision(status=PromotionStatus.VERIFIED if result.evidence_ids else PromotionStatus.NO_VALUE, reason="Evidence recorded without a new fact.")


class EvidenceLedgerService:
    async def record(self, session: AsyncSession, payload: EvidenceLedgerContract) -> EvidenceLedger:
        if not payload.artifact_id or not payload.tool_call_id or not payload.agent_task_id:
            raise DomainError("EVIDENCE_CHAIN_INCOMPLETE", "Evidence must link to Artifact, ToolCall, and AgentTask.")
        artifact = await session.get(Artifact, payload.artifact_id)
        tool_call = await session.get(ToolCall, payload.tool_call_id)
        task = await session.get(AgentTask, payload.agent_task_id)
        if (
            artifact is None
            or tool_call is None
            or task is None
            or artifact.run_id != payload.run_id
            or tool_call.run_id != payload.run_id
            or task.run_id != payload.run_id
            or artifact.tool_call_id != tool_call.id
        ):
            raise DomainError("EVIDENCE_CHAIN_INVALID", "Evidence sources must exist and belong to the same Run.")
        if payload.source_chain and payload.source_chain[-3:] != [payload.artifact_id, payload.tool_call_id, payload.agent_task_id]:
            raise DomainError("EVIDENCE_CHAIN_INVALID", "Evidence source chain is not in Artifact -> ToolCall -> AgentTask order.")
        item = EvidenceLedger(
            id=payload.evidence_id, run_id=payload.run_id, evidence_type=payload.evidence_type,
            artifact_id=payload.artifact_id, tool_call_id=payload.tool_call_id, agent_task_id=payload.agent_task_id,
            summary=payload.summary[:4000], sha256=payload.sha256 or _digest(payload.summary), status=payload.status,
            retention_class=payload.retention_class, source_chain=payload.source_chain or [payload.artifact_id, payload.tool_call_id, payload.agent_task_id],
        )
        session.add(item)
        await session.flush()
        return item


class MemoryCenter:
    """Short structured snapshots; no full agent conversation history."""

    async def write_snapshot(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        stage: str,
        working_memory: dict,
        verified_fact_ids: list[str],
        hypothesis_ids: list[str],
        evidence_ids: list[str],
        created_by_task_id: str | None = None,
    ) -> MemorySnapshot:
        current = list(
            (
                await session.scalars(
                    select(MemorySnapshot).where(
                        MemorySnapshot.run_id == run_id, MemorySnapshot.is_current.is_(True)
                    )
                )
            ).all()
        )
        for item in current:
            item.is_current = False
        version = int(
            await session.scalar(select(func.max(MemorySnapshot.version)).where(MemorySnapshot.run_id == run_id))
            or 0
        ) + 1
        snapshot = MemorySnapshot(
            run_id=run_id,
            version=version,
            stage=stage,
            working_memory_json={str(k): v for k, v in working_memory.items()},
            verified_fact_ids_json=list(verified_fact_ids),
            hypothesis_ids_json=list(hypothesis_ids),
            evidence_ids_json=list(evidence_ids),
            created_by_task_id=created_by_task_id,
            is_current=True,
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def read_snapshot(self, session: AsyncSession, run_id: str) -> MemorySnapshot | None:
        return await session.scalar(
            select(MemorySnapshot)
            .where(MemorySnapshot.run_id == run_id, MemorySnapshot.is_current.is_(True))
            .order_by(MemorySnapshot.version.desc())
        )

    async def read_for_role(self, session: AsyncSession, run_id: str, role: AgentRole | str) -> dict:
        snapshot = await self.read_snapshot(session, run_id)
        policy = await session.scalar(
            select(AgentRolePolicy).where(AgentRolePolicy.role == AgentRole(role).value)
        )
        readable = set(policy.readable_memory_types_json if policy else [])
        if snapshot is None:
            return {"stage": None, "working_memory": {}, "verified_fact_ids": [], "hypothesis_ids": [], "evidence_ids": [], "version": 0}
        return {
            "stage": snapshot.stage,
            "version": snapshot.version,
            "working_memory": snapshot.working_memory_json if "WORKING" in readable else {},
            "verified_fact_ids": snapshot.verified_fact_ids_json if "VERIFIED_FACT" in readable else [],
            "hypothesis_ids": snapshot.hypothesis_ids_json if "HYPOTHESIS" in readable else [],
            "evidence_ids": snapshot.evidence_ids_json if "EVIDENCE" in readable else [],
        }


class SolutionChainService:
    async def accept(self, session: AsyncSession, node: SolutionChainNodeContract) -> SolutionChainNode:
        if not node.evidence_ids or not node.result_fact_ids or not node.capability_added.strip():
            raise DomainError(
                "SOLUTION_NODE_NOT_SUPPORTED",
                "A solution node needs capability, result facts, and evidence.",
            )
        task = await session.get(AgentTask, node.agent_task_id)
        if task is None or task.run_id != node.run_id or task.status != AgentTaskStatus.COMPLETED.value:
            raise DomainError(
                "SOLUTION_NODE_TASK_INVALID",
                "The solution node task must be a completed task in this run.",
            )
        facts = list(
            (
                await session.scalars(
                    select(VerifiedFact).where(
                        VerifiedFact.run_id == node.run_id,
                        VerifiedFact.id.in_(node.result_fact_ids),
                    )
                )
            ).all()
        )
        evidence = list(
            (
                await session.scalars(
                    select(EvidenceLedger).where(
                        EvidenceLedger.run_id == node.run_id,
                        EvidenceLedger.id.in_(node.evidence_ids),
                    )
                )
            ).all()
        )
        if len(facts) != len(set(node.result_fact_ids)) or len(evidence) != len(set(node.evidence_ids)):
            raise DomainError(
                "SOLUTION_NODE_CHAIN_INCOMPLETE",
                "Solution node references facts or evidence outside the run.",
            )
        existing = await session.scalar(
            select(SolutionChainNode).where(
                SolutionChainNode.run_id == node.run_id,
                SolutionChainNode.node_id == node.node_id,
            )
        )
        if existing:
            return existing
        item = SolutionChainNode(
            run_id=node.run_id,
            node_id=node.node_id,
            stage=node.stage,
            objective=node.objective,
            input_fact_ids_json=node.input_fact_ids,
            agent_task_id=node.agent_task_id,
            logical_tool_call_id=node.logical_tool_call_id,
            result_fact_ids_json=node.result_fact_ids,
            capability_added=node.capability_added,
            evidence_ids_json=node.evidence_ids,
            status="ACCEPTED",
        )
        session.add(item)
        await session.flush()
        return item


class DeterministicController:
    """Only owner of task lifecycle, promotions, and multi-agent stage moves."""

    def __init__(self) -> None:
        self.permissions = AgentPermissionController()
        self.promotion_gate = PromotionGate()
        self.evidence = EvidenceLedgerService()
        self.memory = MemoryCenter()
        self.solution_chain = SolutionChainService()

    async def seed_policies(self, session: AsyncSession) -> None:
        for contract in DEFAULT_POLICIES:
            policy = await session.scalar(select(AgentRolePolicy).where(AgentRolePolicy.role == contract.role.value))
            values = contract.model_dump()
            values["role"] = contract.role.value
            for key in ("readable_memory_types", "allowed_tool_types", "allowed_tools", "allowed_outputs", "forbidden_operations"):
                values[f"{key}_json"] = values.pop(key)
            if policy is None:
                session.add(AgentRolePolicy(**values))
            else:
                for key, value in values.items():
                    if key not in {"role"}:
                        setattr(policy, key, value)
        await session.flush()

    async def create_task(self, session: AsyncSession, task: AgentTaskContract) -> AgentTask:
        policy = await self.permissions.policy(session, task.agent_role)
        for tool in task.allowed_tools:
            self.permissions.check_tool(policy, tool)
        budget = task.budget.model_dump()
        if budget["max_logical_calls"] > policy.max_logical_calls or budget["max_internal_requests"] > policy.max_internal_requests or budget["max_runtime_seconds"] > policy.max_runtime_seconds:
            raise DomainError("AGENT_BUDGET_EXCEEDED", "Task budget exceeds the role policy.")
        item = AgentTask(
            id=task.task_id, run_id=task.run_id, agent_role=task.agent_role.value, objective=task.objective,
            task_kind=task.task_kind.value,
            known_fact_ids_json=task.known_fact_ids, active_hypothesis_ids_json=task.active_hypothesis_ids,
            allowed_tools_json=task.allowed_tools, budget_json=budget, success_condition=task.success_condition,
            stop_conditions_json=task.stop_conditions, evidence_snapshot_id=task.evidence_snapshot_id,
            created_by_task_id=task.created_by_task_id, status=AgentTaskStatus.PENDING.value,
            timeout_seconds=task.timeout_seconds, input_snapshot_version=task.input_snapshot_version,
            result_schema_version=task.result_schema_version,
            context_json=task.context,
        )
        session.add(item)
        await session.flush()
        return item

    async def authorize_tool(self, session: AsyncSession, task_id: str, tool_name: str, logical_calls_used: int = 0) -> AgentTask:
        """Check role, task lease/state, cancel flag, and logical budget."""
        task = await session.get(AgentTask, task_id)
        if task is None:
            raise DomainError("AGENT_TASK_NOT_FOUND", "Agent task does not exist.")
        if task.status != AgentTaskStatus.RUNNING.value:
            raise DomainError("AGENT_TASK_NOT_RUNNING", "Tools can only run for a leased task.")
        if task.cancel_requested:
            raise DomainError("AGENT_TASK_CANCELLED", "The controller cancelled this task.")
        policy = await self.permissions.policy(session, task.agent_role)
        self.permissions.check_tool(policy, tool_name)
        if logical_calls_used >= int((task.budget_json or {}).get("max_logical_calls", 0)):
            raise DomainError("AGENT_BUDGET_EXCEEDED", "The task logical-call budget is exhausted.")
        return task

    async def claim_task(self, session: AsyncSession, task_id: str, owner: str, lease_seconds: int = 120) -> str:
        task = await session.get(AgentTask, task_id, with_for_update=True)
        if task is None:
            raise DomainError("AGENT_TASK_NOT_FOUND", "Agent task does not exist.")
        now = datetime.now(UTC)
        if task.status == AgentTaskStatus.RUNNING.value and task.lease_expires_at and task.lease_expires_at > now:
            raise DomainError("AGENT_TASK_LEASED", "Agent task is leased by another worker.")
        if task.status not in {AgentTaskStatus.PENDING.value, AgentTaskStatus.RUNNING.value}:
            raise DomainError("AGENT_TASK_NOT_CLAIMABLE", "Only pending or expired tasks can be claimed.")
        token = f"lease-{uuid4()}"
        task.status, task.lease_owner, task.lease_token = AgentTaskStatus.RUNNING.value, owner, token
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.optimistic_version += 1
        await session.flush()
        return token

    async def request_cancel(self, session: AsyncSession, task_id: str) -> None:
        task = await session.get(AgentTask, task_id)
        if task is None:
            raise DomainError("AGENT_TASK_NOT_FOUND", "Agent task does not exist.")
        task.cancel_requested = True
        task.optimistic_version += 1

    async def complete_task(self, session: AsyncSession, task_id: str, result: AgentTaskResultContract, lease_token: str | None = None) -> PromotionDecision:
        task = await session.get(AgentTask, task_id, with_for_update=True)
        if task is None:
            raise DomainError("AGENT_TASK_NOT_FOUND", "Agent task does not exist.")
        if result.task_id != task.id:
            raise DomainError("AGENT_TASK_RESULT_MISMATCH", "Task result does not belong to the task.")
        if task.status != AgentTaskStatus.RUNNING.value:
            raise DomainError("AGENT_TASK_NOT_RUNNING", "Only a running task can submit a result.")
        if task.lease_token and lease_token != task.lease_token:
            raise DomainError("AGENT_TASK_LEASE_INVALID", "The task lease is invalid or missing.")
        if task.cancel_requested:
            result = result.model_copy(update={"status": AgentTaskStatus.CANCELLED})
        stored = AgentTaskResult(
            task_id=task.id, status=result.status.value, new_facts_json=result.new_facts,
            updated_hypotheses_json=result.updated_hypotheses, evidence_ids_json=result.evidence_ids,
            accepted_solution_steps_json=result.accepted_solution_steps, rejected_paths_json=result.rejected_paths,
            failure_classification_json=result.failure_classification.model_dump() if result.failure_classification else None,
            proposed_next_action_json=result.proposed_next_action, handoff_summary=result.handoff_summary,
            schema_version=result.schema_version,
        )
        session.add(stored)
        task.status = result.status.value
        task.optimistic_version += 1
        task.lease_expires_at = None
        if result.failure_classification:
            await self.record_failure(session, task.run_id, result.failure_classification.model_dump())
        decision = await self.promotion_gate.promote_result(session, task, result)
        await session.flush()
        return decision

    async def record_failure(self, session: AsyncSession, run_id: str, failure: dict[str, Any]) -> FailureSignature:
        fingerprint = str(failure["fingerprint"])
        signature = await session.scalar(select(FailureSignature).where(FailureSignature.run_id == run_id, FailureSignature.fingerprint == fingerprint))
        if signature is None:
            signature = FailureSignature(
                run_id=run_id, fingerprint=fingerprint, classification=str(failure["classification"]), retryable=bool(failure.get("retryable", False)),
                reason=str(failure.get("reason", "")), next_allowed_condition=str(failure.get("next_allowed_condition", "")),
            )
            session.add(signature)
        else:
            signature.attempt_count += 1
            signature.last_seen_at = datetime.now(UTC)
            signature.retryable = bool(failure.get("retryable", signature.retryable))
        await session.flush()
        return signature

    async def finalize_verified_candidate(
        self,
        session: AsyncSession,
        run: SolveRun,
        *,
        candidate: str,
        verify_task_id: str,
        source_artifact_id: str,
        producing_tool_call_id: str,
        evidence_ids: list[str],
        pattern_matched: bool,
        fresh_reproduction: bool,
        assistance_level: str = "AUTONOMOUS",
    ) -> FlagCandidate:
        """The only multi-agent path allowed to promote a candidate to solved."""
        task = await session.get(AgentTask, verify_task_id)
        artifact = await session.get(Artifact, source_artifact_id)
        tool_call = await session.get(ToolCall, producing_tool_call_id)
        if task is None or task.run_id != run.id or task.agent_role != AgentRole.VERIFY.value:
            raise DomainError("FLAG_VERIFY_TASK_INVALID", "Flag verification must use an isolated Verify task.")
        if artifact is None or artifact.run_id != run.id or tool_call is None or tool_call.run_id != run.id:
            raise DomainError("FLAG_SOURCE_INVALID", "Flag source artifact and producing tool call are not valid.")
        evidence = list(
            (
                await session.scalars(
                    select(EvidenceLedger).where(
                        EvidenceLedger.run_id == run.id,
                        EvidenceLedger.id.in_(evidence_ids),
                        EvidenceLedger.artifact_id == artifact.id,
                        EvidenceLedger.tool_call_id == tool_call.id,
                        EvidenceLedger.agent_task_id == task.id,
                    )
                )
            ).all()
        )
        if (
            not candidate
            or not pattern_matched
            or not fresh_reproduction
            or len(evidence) != len(set(evidence_ids))
            or assistance_level not in {"AUTONOMOUS", "HINT_GUIDED", "EVIDENCE_GUIDED"}
        ):
            raise DomainError("FLAG_PROMOTION_REJECTED", "Candidate does not satisfy the complete verification gate.")
        item = await session.scalar(
            select(FlagCandidate).where(
                FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate
            )
        )
        if item is None:
            item = FlagCandidate(
                run_id=run.id,
                candidate=candidate,
                source_artifact_id=artifact.id,
                pattern_matched=True,
                verified=True,
                review_state="VALID",
                first_seen_source_type="TOOL_ARTIFACT",
                first_seen_source_id=artifact.id,
                first_seen_at=datetime.now(UTC),
                source_tool_call_id=tool_call.id,
                source_agent_task_id=None,
                source_assistance_level=assistance_level,
            )
            session.add(item)
            await session.flush()
            session.add(
                FlagProvenance(
                    run_id=run.id,
                    candidate_id=item.id,
                    first_seen_source_type="TOOL_ARTIFACT",
                    first_seen_source_id=artifact.id,
                    first_seen_at=item.first_seen_at or datetime.now(UTC),
                    source_artifact_id=artifact.id,
                    source_tool_call_id=tool_call.id,
                    source_assistance_level=assistance_level,
                    source_is_autonomous=assistance_level in {"AUTONOMOUS", "HINT_GUIDED"},
                    verification_source_type="FRESH_REPRODUCTION",
                    verification_source_id=artifact.id,
                )
            )
        else:
            item.source_artifact_id = artifact.id
            item.pattern_matched = True
            item.verified = True
            item.review_state = "VALID"
            provenance = await session.scalar(select(FlagProvenance).where(FlagProvenance.candidate_id == item.id))
            if provenance:
                provenance.verification_source_type = "FRESH_REPRODUCTION"
                provenance.verification_source_id = artifact.id
        run.fresh_reproduction_verified = True
        if RunStatus(run.status) == RunStatus.VERIFYING_FLAG:
            transition(run, RunStatus.REPORTING)
        if RunStatus(run.status) == RunStatus.REPORTING:
            transition(run, RunStatus.COMPLETED_SOLVED)
        run.controller_revision += 1
        from app.services.temporary_data import temporary_data_janitor

        await temporary_data_janitor.cleanup_terminal_run(session, run)
        await session.flush()
        return item

    @staticmethod
    def validate_review(proposal: PlannerProposalContract, review: AnalysisReviewContract) -> None:
        if proposal.proposal_id != review.proposal_id:
            raise DomainError("ANALYSIS_REVIEW_MISMATCH", "Review does not belong to proposal.")
        if review.decision == AnalysisDecision.APPROVE:
            if not review.question_being_tested.strip():
                raise DomainError("ANALYSIS_QUESTION_REQUIRED", "An approved review must state the decision question.")
            if not review.supporting_evidence_ids:
                raise DomainError("ANALYSIS_EVIDENCE_REQUIRED", "An approved proposal requires supporting evidence.")
            if not review.expected_true_signal or not review.expected_false_signal:
                raise DomainError("ANALYSIS_SIGNAL_CONTROLS_REQUIRED", "An approved review must define both true and false signals.")
            if review.recommended_tool and review.recommended_tool not in proposal.allowed_tools:
                raise DomainError("ANALYSIS_TOOL_NOT_IN_PROPOSAL", "The approved tool must be declared by the planner.")
            if review.independent_variable and review.independent_variable in review.required_controls:
                raise DomainError("ANALYSIS_CONTROL_VARIABLE_INVALID", "The independent variable cannot also be a control.")
            if any(not str(value).strip() for value in review.required_controls.values()):
                raise DomainError("ANALYSIS_CONTROL_INVALID", "Every experiment control must have a concrete value.")
        if "sqlmap_run" in proposal.allowed_tools:
            raise DomainError("PLANNER_TOOL_FORBIDDEN", "Planner cannot schedule direct exploitation execution.")

    async def advance_stage(self, session: AsyncSession, run: SolveRun, stage: str) -> None:
        """Map multi-agent stages onto the existing guarded Run state machine."""
        mapping = {"INTAKE": RunStatus.PREPARING, "RECON": RunStatus.ANALYZING, "ANALYSIS": RunStatus.PLANNING, "EXPLOITATION": RunStatus.EXECUTING, "VERIFICATION": RunStatus.VERIFYING_FLAG, "REPORTING": RunStatus.REPORTING}
        target = mapping.get(stage)
        if target is None:
            raise DomainError("AGENT_STAGE_INVALID", "Unknown multi-agent stage.", {"stage": stage})
        current = RunStatus(run.status)
        if current != target:
            transition(run, target)
        run.solver_mode = "multi_agent_v1"
        run.controller_revision += 1
        await session.flush()


deterministic_controller = DeterministicController()
