"""Durable, model-backed multi-agent controller.

The controller is intentionally policy-oriented: it creates and leases tasks,
persists model contracts, records evidence, and applies the finish gate.  It
does not invent a fixed GET sequence and it never treats a non-empty evidence
list as an automatic Analysis approval.
"""

from __future__ import annotations

import uuid
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AnalysisReview, AgentTask, ApprovedAction, EvidenceLedger, PlannerProposal, VerifiedFact
from app.models.run import Artifact, FlagCandidate, Hypothesis, Observation, RunAttempt, RunExecutionLease, SolveRun, ToolCall
from app.orchestration.role_agent_runtime import RoleAgentRuntime
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskContract,
    AgentTaskKind,
    AgentTaskResultContract,
    AgentTaskStatus,
    AnalysisDecision,
    AnalysisReviewContract,
    EvidenceLedgerContract,
    PlannerProposalContract,
    TaskBudget,
)
from app.services.events import event_service
from app.services.multi_agent import deterministic_controller
from app.services.solver_state import solver_state_service
from app.tools.gateway import tool_gateway

logger = logging.getLogger(__name__)


class MultiAgentOrchestrator:
    def __init__(self, tool_invoker=None, runtime: RoleAgentRuntime | None = None) -> None:
        self.tool_invoker = tool_invoker or tool_gateway.invoke
        self.runtime = runtime or RoleAgentRuntime(tool_invoker=self.tool_invoker)

    async def _status(self, session, run: SolveRun, target: RunStatus) -> None:
        if RunStatus(run.status) == target:
            return
        transition(run, target)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status, "controller": "multi_agent_v1"})

    async def _phase(self, session, run: SolveRun, phase: str) -> None:
        if str(run.current_phase or "") == phase:
            return
        previous = run.current_phase
        run.current_phase = phase
        await session.commit()
        await event_service.append(session, run.id, "run.phase_changed", {"previous_phase": previous, "phase": phase, "source": "role_agent_runtime"})

    async def _capability_phase(self, session, run: SolveRun) -> str:
        """Derive the next phase from durable evidence/capabilities, not role completion."""
        state = await solver_state_service.load(session, run.id)
        ledger = state.capability_ledger_json if state else {}
        candidate = await self._candidate_gate(session, run)
        if candidate:
            return "FLAG_VERIFICATION"
        keys = {str(key).lower() for key in ledger}
        if any("metadata" in key or "extraction" in key or "flag_search" in key for key in keys):
            return "FLAG_SEARCH"
        if any("boolean" in key or "oracle" in key for key in keys):
            return "CHAINING"
        evidence_count = int(await session.scalar(select(func.count(EvidenceLedger.id)).where(EvidenceLedger.run_id == run.id)) or 0)
        if evidence_count:
            hypothesis_count = int(await session.scalar(select(func.count()).select_from(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.status.in_(["OPEN", "ACTIVE"]))) or 0)
            return "HYPOTHESIS" if hypothesis_count else "MAPPING"
        return "BASELINE"

    async def _task(self, session, run: SolveRun, role: AgentRole, kind: AgentTaskKind, objective: str, tools: list[str], *, parent: str | None = None, context: dict | None = None, budget: TaskBudget | None = None, success_condition: str = "produce a validated structured handoff with evidence or an explicit failure classification", stop_conditions: list[str] | None = None) -> tuple[AgentTask, str]:
        logger.warning("multi_agent.task.snapshot.begin run_id=%s role=%s", run.id, role.value)
        snapshot = await deterministic_controller.memory.read_snapshot(session, run.id)
        logger.warning("multi_agent.task.snapshot.done run_id=%s snapshot_id=%s", run.id, snapshot.id if snapshot else None)
        logger.warning("multi_agent.task.policy.begin run_id=%s role=%s", run.id, role.value)
        policy = await deterministic_controller.permissions.policy(session, role)
        logger.warning("multi_agent.task.policy.done run_id=%s role=%s", run.id, role.value)
        selected_budget = budget or TaskBudget(max_logical_calls=0, max_internal_requests=min(8, policy.max_internal_requests), max_runtime_seconds=min(300, policy.max_runtime_seconds))
        max_calls = min(int(selected_budget.max_logical_calls), int(policy.max_logical_calls)) if tools else 0
        contract = AgentTaskContract(
            task_id=f"AT-{role.value}-{uuid.uuid4().hex[:12]}", run_id=run.id, agent_role=role, task_kind=kind,
            objective=objective, allowed_tools=tools, created_by_task_id=parent,
            evidence_snapshot_id=snapshot.id if snapshot else None,
            input_snapshot_version=snapshot.version if snapshot else 0,
            budget=selected_budget.model_copy(update={"max_logical_calls": max_calls}),
            success_condition=success_condition,
            stop_conditions=stop_conditions or ["honor task budget", "stop after one discriminating experiment"], context=context or {},
        )
        logger.warning("multi_agent.task.create.begin run_id=%s task_id=%s role=%s kind=%s parent=%s", run.id, contract.task_id, role.value, kind.value, parent)
        item = await deterministic_controller.create_task(session, contract)
        logger.warning("multi_agent.task.create.flushed run_id=%s task_id=%s", run.id, item.id)
        token = await deterministic_controller.claim_task(session, item.id, "role-agent-runtime", lease_seconds=contract.budget.max_runtime_seconds)
        logger.warning("multi_agent.task.claimed run_id=%s task_id=%s", run.id, item.id)
        return item, token

    async def _evidence_for_task(self, session, run: SolveRun, task: AgentTask) -> tuple[list[str], list[tuple[ToolCall, Artifact]]]:
        pairs: list[tuple[ToolCall, Artifact]] = []
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.agent_task_id == task.id).order_by(ToolCall.created_at))).all())
        for call in calls:
            artifact = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == call.id).order_by(Artifact.created_at.desc()))
            if artifact:
                pairs.append((call, artifact))
        evidence_ids: list[str] = []
        for call, artifact in pairs:
            existing = await session.scalar(select(EvidenceLedger).where(EvidenceLedger.run_id == run.id, EvidenceLedger.tool_call_id == call.id, EvidenceLedger.agent_task_id == task.id))
            if existing:
                evidence_ids.append(existing.id)
                continue
            semantic = {
                "http_request": "HTTP request response",
                "content_discovery": "Public content discovery response",
                "sql_boolean_compare": "SQL TRUE/FALSE oracle comparison response",
                "oracle_probe_matrix": "Bounded oracle probe matrix response",
            }.get(call.tool_name, f"{call.tool_name} tool response")
            item = await deterministic_controller.evidence.record(session, EvidenceLedgerContract(
                evidence_id=f"E-{uuid.uuid4().hex[:12]}", run_id=run.id, evidence_type="TOOL_ARTIFACT", artifact_id=artifact.id,
                tool_call_id=call.id, agent_task_id=task.id, summary=(artifact.summary or semantic)[:4000], sha256=artifact.sha256, source_chain=[artifact.id, call.id, task.id],
            ))
            evidence_ids.append(item.id)
        return evidence_ids, pairs

    async def _complete(self, session, run: SolveRun, task: AgentTask, token: str, result: AgentTaskResultContract) -> AgentTaskResultContract:
        evidence_ids, pairs = await self._evidence_for_task(session, run, task)
        if evidence_ids:
            result = result.model_copy(update={"evidence_ids": sorted(set((result.evidence_ids or []) + evidence_ids))})
        await deterministic_controller.complete_task(session, task.id, result, token)
        await event_service.append(session, run.id, "agent.task.completed", {"task_id": task.id, "agent_role": task.agent_role, "task_kind": task.task_kind, "status": result.status.value, "evidence_ids": result.evidence_ids})
        return result

    async def _proposal(self, session, run: SolveRun, task: AgentTask, result: AgentTaskResultContract) -> PlannerProposal:
        raw = (result.proposed_next_action or {}).get("proposal") or {}
        try:
            contract = PlannerProposalContract.model_validate(raw)
        except Exception as error:
            raise DomainError("MODEL_OUTPUT_SCHEMA_INVALID", f"PlannerProposalContract is invalid: {error}", {"task_id": task.id}) from error
        # The model-visible proposal_id may repeat on a later Run.  The
        # relational key must stay globally unique because Review and
        # ApprovedAction reference the PlannerProposal row.
        row = PlannerProposal(id=str(uuid.uuid4()), run_id=run.id, proposal_id=contract.proposal_id, current_stage=contract.current_stage, decision_question=contract.decision_question, next_agent=contract.next_agent.value, objective=contract.objective, input_fact_ids_json=contract.input_fact_ids, required_capabilities_json=contract.required_capabilities, allowed_tools_json=contract.allowed_tools, budget_json=contract.budget.model_dump(), success_condition=contract.success_condition, stop_conditions_json=contract.stop_conditions, fallback=contract.fallback, created_by_task_id=task.id)
        session.add(row)
        await session.flush()
        return row

    async def _review(self, session, run: SolveRun, proposal: PlannerProposal, task: AgentTask, result: AgentTaskResultContract) -> AnalysisReview:
        raw = (result.proposed_next_action or {}).get("review") or {}
        try:
            contract = AnalysisReviewContract.model_validate(raw)
        except Exception as error:
            raise DomainError("MODEL_OUTPUT_SCHEMA_INVALID", f"AnalysisReviewContract is invalid: {error}", {"task_id": task.id}) from error
        proposal_contract = PlannerProposalContract(proposal_id=proposal.proposal_id, run_id=run.id, current_stage=proposal.current_stage, decision_question=proposal.decision_question, next_agent=proposal.next_agent, objective=proposal.objective, input_fact_ids=proposal.input_fact_ids_json, required_capabilities=proposal.required_capabilities_json, allowed_tools=proposal.allowed_tools_json, budget=proposal.budget_json, success_condition=proposal.success_condition, stop_conditions=proposal.stop_conditions_json, fallback=proposal.fallback)
        try:
            deterministic_controller.validate_review(proposal_contract, contract)
        except DomainError as error:
            if contract.decision == AnalysisDecision.APPROVE:
                contract = contract.model_copy(update={"decision": AnalysisDecision.REVISE, "audit_reason": error.code, "reason": error.message})
        row = AnalysisReview(proposal_id=proposal.id, task_kind=contract.task_kind, decision=contract.decision.value, confidence=contract.confidence, question_being_tested=contract.question_being_tested, supporting_evidence_ids_json=contract.supporting_evidence_ids, independent_variable=contract.independent_variable, required_controls_json=contract.required_controls, expected_true_signal_json=contract.expected_true_signal, expected_false_signal_json=contract.expected_false_signal, recommended_tool=contract.recommended_tool, reason=contract.reason, audit_reason=contract.audit_reason, approved_arguments_json=contract.approved_arguments, approved_fact_indexes_json=contract.approved_fact_indexes, approved_evidence_ids_json=contract.approved_evidence_ids, approved_hypothesis_updates_json=contract.approved_hypothesis_updates, capabilities_added_json=contract.capabilities_added, solution_step_accepted=contract.solution_step_accepted, next_phase=contract.next_phase)
        session.add(row)
        await session.flush()
        return row

    async def _approved_action(self, session, run: SolveRun, proposal: PlannerProposal, review: AnalysisReview) -> ApprovedAction:
        if review.decision != AnalysisDecision.APPROVE.value:
            raise DomainError("PLAN_REVIEW_NOT_APPROVED", "Only an APPROVE PLAN_REVIEW can issue an ApprovedAction.")
        budget = proposal.budget_json or {}
        approved_id = f"AA-{uuid.uuid4().hex[:12]}"
        item = ApprovedAction(
            id=approved_id, run_id=run.id, approved_action_id=approved_id,
            proposal_id=proposal.id, analysis_review_id=review.id, agent_role=proposal.next_agent,
            tool_name=(review.recommended_tool or (proposal.allowed_tools_json or [""])[0]),
            argument_constraints_json=review.approved_arguments_json or {},
            max_logical_calls=max(1, int(budget.get("max_logical_calls") or 1)),
            expires_at=datetime.now(UTC) + timedelta(seconds=min(300, int(budget.get("max_runtime_seconds") or 300))), status="ACTIVE",
        )
        # The row is attached by _persist_plan_review after the production
        # task has been constructed.  This lets SQLAlchemy order all parent
        # and child inserts in one explicit flush on MySQL.
        return item

    async def _memory(self, session, run: SolveRun, *, stage: str, task: AgentTask, working: dict) -> None:
        from app.models.multi_agent import VerifiedFact
        from app.models.run import Hypothesis
        fact_result = await session.scalars(select(VerifiedFact.id).where(VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"))
        facts = list(fact_result.all())
        hypothesis_result = await session.scalars(select(Hypothesis.id).where(Hypothesis.run_id == run.id, Hypothesis.status.in_(["OPEN", "ACTIVE"])))
        hypotheses = list(hypothesis_result.all())
        evidence_result = await session.scalars(select(EvidenceLedger.id).where(EvidenceLedger.run_id == run.id))
        evidence = list(evidence_result.all())
        state = await solver_state_service.load(session, run.id)
        candidate_result = await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.verified.is_(False)))
        working = {**working, "capability_ledger": (state.capability_ledger_json if state else {}), "unverified_candidates": [item.id for item in candidate_result.all()]}
        await deterministic_controller.memory.write_snapshot(session, run.id, stage=stage, working_memory=working, verified_fact_ids=facts, hypothesis_ids=hypotheses, evidence_ids=sorted(set(evidence)), created_by_task_id=task.id)

    async def _fail_plan_review_persistence(
        self,
        session,
        run_id: str,
        task_id: str,
        lease_token: str,
        reason: str,
    ) -> None:
        """Leave no completed Analysis task without its durable review.

        This runs after the plan-review transaction has been rolled back.  It
        records the task failure and checkpoint in a fresh transaction so the
        controller cannot strand a successful-looking PLAN_REVIEW task.
        """
        await session.rollback()
        run = await session.get(SolveRun, run_id)
        task = await session.get(AgentTask, task_id)
        if task and task.status == AgentTaskStatus.RUNNING.value:
            failure = AgentTaskResultContract(
                task_id=task.id,
                status=AgentTaskStatus.FAILED,
                failure_classification={
                    "fingerprint": "plan-review-persistence-incomplete",
                    "classification": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE",
                    "retryable": True,
                    "reason": reason,
                    "next_allowed_condition": "repair durable plan-review persistence and create a fresh task lease",
                },
                handoff_summary=reason[:4000],
            )
            await deterministic_controller.complete_task(session, task.id, failure, lease_token)
        if run and RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
            run.last_error_code = "PLAN_REVIEW_PERSISTENCE_INCOMPLETE"
            run.last_error_message = reason[:4000]
            run.recovery_checkpoint_json = {"classification": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "analysis_task_id": task_id}
            await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
        if run:
            await event_service.append(
                session,
                run.id,
                "analysis.plan_review_persistence_failed",
                {"task_id": task_id, "code": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "reason": reason[:1000]},
            )

    async def _persist_plan_review(
        self,
        session,
        run: SolveRun,
        proposal: PlannerProposal,
        plan_task: AgentTask,
        plan_token: str,
        plan_result: AgentTaskResultContract,
    ) -> tuple[AnalysisReview, ApprovedAction | None, AgentTask | None, str | None]:
        """Persist PLAN_REVIEW and its dispatch as one controller transaction.

        The Analysis task is intentionally completed last.  If any review,
        approval, task creation, memory snapshot, or completion write fails,
        the caller rolls back the entire chain and checkpoints the Run.
        """
        if plan_result.status != AgentTaskStatus.COMPLETED:
            await deterministic_controller.complete_task(session, plan_task.id, plan_result, plan_token)
            await session.commit()
            raise DomainError("MODEL_OUTPUT_SCHEMA_INVALID", plan_result.handoff_summary or "Analysis did not return a valid PLAN_REVIEW contract.")

        logger.warning("multi_agent.plan_review.persist.begin run_id=%s task_id=%s proposal_id=%s", run.id, plan_task.id, proposal.proposal_id)
        review = await self._review(session, run, proposal, plan_task, plan_result)
        logger.warning("multi_agent.plan_review.review_flushed run_id=%s review_id=%s decision=%s", run.id, review.id, review.decision)
        approved: ApprovedAction | None = None
        production_task: AgentTask | None = None
        production_token: str | None = None
        if review.decision == AnalysisDecision.APPROVE.value:
            role = AgentRole(proposal.next_agent)
            if role == AgentRole.VERIFY and not await self._candidate_gate(session, run):
                # A model may approve a Verify proposal optimistically, but
                # the controller must not create a Verify task without the
                # complete candidate -> artifact -> ToolCall chain.
                review.decision = AnalysisDecision.REVISE.value
                review.audit_reason = "VERIFY_GATE_REQUIRES_CANDIDATE_ARTIFACT_TOOLCALL"
                review.reason = "Verification is deferred until a durable flag candidate has a source artifact and ToolCall."
            tools = list(proposal.allowed_tools_json or [])
            if review.decision == AnalysisDecision.APPROVE.value and role == AgentRole.VERIFY:
                tools = [tool for tool in tools if tool in {"http_request", "script_run"}]
            if review.decision == AnalysisDecision.APPROVE.value and not tools:
                raise DomainError("PROPOSAL_HAS_NO_EXECUTABLE_TOOL", "Approved proposal has no executable tool.")
            if review.decision == AnalysisDecision.APPROVE.value:
                logger.warning("multi_agent.plan_review.approved_action.begin run_id=%s proposal_row_id=%s review_id=%s", run.id, proposal.id, review.id)
                approved = await self._approved_action(session, run, proposal, review)
                logger.warning("multi_agent.plan_review.approved_action.flushed run_id=%s approved_action_id=%s", run.id, approved.id)
                task_kind = {AgentRole.RECON: AgentTaskKind.RECON, AgentRole.EXPLOIT: AgentTaskKind.EXPLOIT, AgentRole.VERIFY: AgentTaskKind.VERIFY}.get(role)
                if task_kind is None:
                    raise DomainError("AGENT_ROLE_INVALID", "PLAN_REVIEW can only dispatch RECON, EXPLOIT, or VERIFY.")
                logger.warning("multi_agent.plan_review.production_task.begin run_id=%s parent_task_id=%s", run.id, plan_task.id)
                production_task, production_token = await self._task(
                    session,
                    run,
                    role,
                    task_kind,
                    proposal.objective,
                    tools,
                    parent=plan_task.id,
                    context={
                        "proposal_id": proposal.proposal_id,
                        "tool": tools[0],
                        "approved_arguments": review.approved_arguments_json,
                        "approved_action_id": approved.id,
                        "logical_calls_used": 0,
                    },
                    budget=TaskBudget.model_validate(proposal.budget_json),
                    success_condition=proposal.success_condition,
                    stop_conditions=proposal.stop_conditions_json,
                )
                logger.warning("multi_agent.plan_review.production_task_flushed run_id=%s analysis_task_id=%s production_task_id=%s", run.id, plan_task.id, production_task.id)
                session.add(approved)
                await session.flush()
                logger.warning("multi_agent.plan_review.approved_action.persisted run_id=%s approved_action_id=%s", run.id, approved.id)

        await self._memory(
            session,
            run,
            stage=review.next_phase if review.decision == AnalysisDecision.APPROVE.value else "HYPOTHESIS",
            task=plan_task,
            working={"proposal": proposal.proposal_id, "plan_review": review.decision, "approved_action_id": approved.id if approved else None},
        )
        logger.warning("multi_agent.plan_review.memory_flushed run_id=%s analysis_task_id=%s", run.id, plan_task.id)
        # This is deliberately the final write in the transaction.  A
        # completed Analysis task is durable evidence that its Review exists;
        # APPROVE additionally proves an ApprovedAction and production task.
        await deterministic_controller.complete_task(session, plan_task.id, plan_result, plan_token)
        logger.warning("multi_agent.plan_review.analysis_completed run_id=%s analysis_task_id=%s", run.id, plan_task.id)
        if review.decision == AnalysisDecision.APPROVE.value and (approved is None or production_task is None):
            raise DomainError("PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "Approved PLAN_REVIEW has no dispatched production task.")
        await session.commit()
        logger.warning("multi_agent.plan_review.transaction_committed run_id=%s analysis_task_id=%s review_id=%s", run.id, plan_task.id, review.id)

        await event_service.append(session, run.id, "analysis.review.created", {"proposal_id": proposal.proposal_id, "analysis_review_id": review.id, "task_kind": review.task_kind, "decision": review.decision})
        if approved and production_task:
            await event_service.append(session, run.id, "approved_action.created", {"proposal_id": proposal.proposal_id, "analysis_review_id": review.id, "approved_action_id": approved.id, "agent_role": approved.agent_role, "tool": approved.tool_name})
            await event_service.append(session, run.id, "agent.task.created", {"task_id": production_task.id, "agent_role": production_task.agent_role, "task_kind": production_task.task_kind, "proposal_id": proposal.proposal_id})
            await event_service.append(session, run.id, "agent.task.claimed", {"task_id": production_task.id, "agent_role": production_task.agent_role, "task_kind": production_task.task_kind})
        return review, approved, production_task, production_token

    async def _result_context(self, session, run: SolveRun, proposal: PlannerProposal, task: AgentTask, result: AgentTaskResultContract) -> dict:
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.agent_task_id == task.id).order_by(ToolCall.created_at))).all())
        rows = []
        for call in calls:
            artifact = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == call.id).order_by(Artifact.created_at.desc()))
            observation = await session.scalar(select(Observation).where(Observation.run_id == run.id, Observation.tool_call_id == call.id).order_by(Observation.created_at.desc()))
            rows.append({"tool_call_id": call.id, "tool": call.tool_name, "arguments_summary": call.arguments_json or {}, "status": call.status, "observation_id": observation.id if observation else None, "artifact_id": artifact.id if artifact else None, "artifact_sha256": artifact.sha256 if artifact else None, "model_view": observation.facts_json if observation else {}})
        state = await solver_state_service.load(session, run.id)
        verified = list((await session.scalars(select(VerifiedFact.id).where(VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"))).all())
        from app.models.run import Hypothesis
        plan_row = await session.scalar(select(AnalysisReview).where(AnalysisReview.proposal_id == proposal.id, AnalysisReview.task_kind == AgentTaskKind.PLAN_REVIEW.value))
        plan_review = {
            "review_id": plan_row.id if plan_row else None,
            "decision": plan_row.decision if plan_row else None,
            "confidence": plan_row.confidence if plan_row else None,
            "question_being_tested": plan_row.question_being_tested if plan_row else None,
            "independent_variable": plan_row.independent_variable if plan_row else None,
            "required_controls": plan_row.required_controls_json if plan_row else {},
            "expected_true_signal": plan_row.expected_true_signal_json if plan_row else {},
            "expected_false_signal": plan_row.expected_false_signal_json if plan_row else {},
            "recommended_tool": plan_row.recommended_tool if plan_row else None,
            "approved_arguments": plan_row.approved_arguments_json if plan_row else {},
            "approved_fact_indexes": plan_row.approved_fact_indexes_json if plan_row else [],
            "approved_evidence_ids": plan_row.approved_evidence_ids_json if plan_row else [],
            "capabilities_added": plan_row.capabilities_added_json if plan_row else [],
            "next_phase": plan_row.next_phase if plan_row else None,
            "reason": plan_row.reason if plan_row else None,
        }
        hypotheses = list((await session.scalars(select(Hypothesis.id).where(Hypothesis.run_id == run.id, Hypothesis.status.in_(["OPEN", "ACTIVE"])))) .all())
        return {
            "proposal": {"proposal_id": proposal.proposal_id, "current_stage": proposal.current_stage, "decision_question": proposal.decision_question, "next_agent": proposal.next_agent, "objective": proposal.objective, "allowed_tools": proposal.allowed_tools_json, "budget": proposal.budget_json, "success_condition": proposal.success_condition, "stop_conditions": proposal.stop_conditions_json},
            "plan_review": plan_review,
            "task_result": result.model_dump(mode="json"), "tool_calls": rows, "response_signatures": [row["model_view"] for row in rows],
            "current_verified_facts": verified,
            "current_capabilities": state.capability_ledger_json if state else {},
            "current_hypotheses": hypotheses,
        }

    async def _apply_result_review(self, session, run: SolveRun, producing_task: AgentTask, review: AnalysisReview) -> list[str]:
        facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.source_task_id == producing_task.id))).all())
        selected = review.approved_fact_indexes_json or []
        approved_ids: list[str] = []
        for index, fact in enumerate(facts):
            if not selected or index in selected:
                if review.decision == AnalysisDecision.APPROVE.value:
                    fact.promotion_status = "VERIFIED"
                    approved_ids.append(fact.id)
        if review.decision == AnalysisDecision.APPROVE.value:
            for capability in (review.capabilities_added_json or []):
                await solver_state_service.record_capability(session, run.id, capability, evidence={"review_id": review.id})
        return approved_ids

    async def _candidate_gate(self, session, run: SolveRun) -> FlagCandidate | None:
        candidates = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.verified.is_(False)).order_by(FlagCandidate.created_at.desc()))).all())
        for candidate in candidates:
            if not candidate.candidate or not candidate.source_artifact_id or not candidate.source_tool_call_id:
                continue
            artifact = await session.get(Artifact, candidate.source_artifact_id)
            call = await session.get(ToolCall, candidate.source_tool_call_id)
            if artifact and call and artifact.tool_call_id == call.id:
                return candidate
        return None

    async def _fresh_reproduction(self, run: SolveRun, candidate: FlagCandidate, artifact: Artifact | None) -> bool:
        if not artifact:
            return False
        path = Path(run.workspace_path, artifact.file_path)
        return path.is_file() and candidate.candidate in path.read_text(encoding="utf-8", errors="replace")

    async def run(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, lease: RunExecutionLease, *, engine: object | None = None) -> dict:
        await deterministic_controller.seed_policies(session)
        await solver_state_service.initialize(session, run, challenge.challenge_type, [], challenge.name, challenge.description)
        self.runtime.engine = engine or self.runtime.engine
        self.runtime.tool_invoker = self.tool_invoker
        try:
            if RunStatus(run.status) in {RunStatus.CREATED, RunStatus.RUNNING}:
                await self._status(session, run, RunStatus.PREPARING)
                await self._status(session, run, RunStatus.ANALYZING)
                await self._status(session, run, RunStatus.PLANNING)
            elif RunStatus(run.status) in {RunStatus.PAUSED_DEPLOYMENT, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RATE_LIMIT}:
                await self._status(session, run, RunStatus.PLANNING)
            max_cycles = max(3, min(8, int(run.max_agent_steps or 8) // 2))
            parent: str | None = None
            context: dict = {}
            for cycle in range(max_cycles):
                await self._phase(session, run, await self._capability_phase(session, run))
                planner_task, planner_token = await self._task(session, run, AgentRole.PLANNER, AgentTaskKind.PLANNING, "Select the next bounded stage from the current memory snapshot.", [], parent=parent, context=context)
                planner_result = await self._complete(session, run, planner_task, planner_token, await self.runtime.execute(session, run, challenge, attempt, planner_task, planner_token))
                if planner_result.status == AgentTaskStatus.FAILED:
                    run.last_error_code = "MODEL_OUTPUT_SCHEMA_INVALID"
                    run.last_error_message = planner_result.handoff_summary
                    run.recovery_checkpoint_json = {"classification": "MODEL_OUTPUT_SCHEMA_INVALID", "task_id": planner_task.id}
                    await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                    return {"status": run.status, "error_code": run.last_error_code, "agent_tasks": cycle + 1}
                proposal = await self._proposal(session, run, planner_task, planner_result)
                await self._memory(session, run, stage=proposal.current_stage, task=planner_task, working={"proposal": proposal.proposal_id, "decision_question": proposal.decision_question, "cycle": cycle})
                parent = planner_task.id

                # PLAN_REVIEW is a mandatory gate for every production role.
                review_context = {"proposal": {"proposal_id": proposal.proposal_id, "current_stage": proposal.current_stage, "decision_question": proposal.decision_question, "next_agent": proposal.next_agent, "objective": proposal.objective, "allowed_tools": proposal.allowed_tools_json, "success_condition": proposal.success_condition, "stop_conditions": proposal.stop_conditions_json, "budget": proposal.budget_json}, "plan_review": True}
                plan_task, plan_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.PLAN_REVIEW, "Audit this proposal before any production tool call.", [], parent=planner_task.id, context=review_context)
                # Keep the Analysis task RUNNING while its contract is
                # validated and its durable dispatch chain is written.  The
                # old order completed the task first, so a failed Review
                # insert stranded the Run in PLANNING forever.
                plan_result = await self.runtime.execute(session, run, challenge, attempt, plan_task, plan_token)
                try:
                    plan_review, approved, production_task, production_token = await asyncio.wait_for(
                        self._persist_plan_review(session, run, proposal, plan_task, plan_token, plan_result),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError as error:
                    await self._fail_plan_review_persistence(
                        session,
                        run.id,
                        plan_task.id,
                        plan_token,
                        "PLAN_REVIEW persistence/dispatch exceeded the 5 second deadline.",
                    )
                    return {"status": RunStatus.PAUSED_CHECKPOINT.value, "error_code": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "agent_tasks": cycle + 1}
                except DomainError as error:
                    if error.code == "MODEL_OUTPUT_SCHEMA_INVALID" and plan_result.status != AgentTaskStatus.COMPLETED:
                        run.last_error_code = error.code
                        run.last_error_message = error.message[:4000]
                        run.recovery_checkpoint_json = {"classification": error.code, "task_id": plan_task.id}
                        await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                        return {"status": run.status, "error_code": error.code, "agent_tasks": cycle + 1}
                    await self._fail_plan_review_persistence(session, run.id, plan_task.id, plan_token, str(error))
                    return {"status": RunStatus.PAUSED_CHECKPOINT.value, "error_code": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "agent_tasks": cycle + 1}
                if plan_review.decision != AnalysisDecision.APPROVE.value:
                    context = {"replan_reason": f"PLAN_REVIEW_{plan_review.decision}"}
                    await self._memory(session, run, stage="HYPOTHESIS", task=plan_task, working=context | {"plan_review": plan_review.decision})
                    await session.commit()
                    await self._status(session, run, RunStatus.PLANNING)
                    continue
                role = AgentRole(proposal.next_agent)
                if role == AgentRole.VERIFY and not await self._candidate_gate(session, run):
                    context = {"replan_reason": "VERIFY_GATE_REQUIRES_CANDIDATE_ARTIFACT_TOOLCALL"}
                    await self._memory(session, run, stage="FLAG_SEARCH", task=plan_task, working=context)
                    await session.commit()
                    continue
                # _persist_plan_review already created and leased the
                # production task before completing AnalysisTask.
                exec_task, exec_token = production_task, production_token
                if exec_task is None or exec_token is None:
                    raise DomainError("PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "Approved PLAN_REVIEW has no production task lease.")
                await self._phase(session, run, "FLAG_VERIFICATION" if role == AgentRole.VERIFY else proposal.current_stage)
                await self._status(session, run, RunStatus.EXECUTING)
                exec_result = await self._complete(session, run, exec_task, exec_token, await self.runtime.execute(session, run, challenge, attempt, exec_task, exec_token))
                await self._status(session, run, RunStatus.EVALUATING)
                result_payload = await self._result_context(session, run, proposal, exec_task, exec_result)
                result_review_task, result_review_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.RESULT_REVIEW, "Review the complete producing task result, ToolCalls, Artifacts and Evidence.", [], parent=exec_task.id, context=result_payload)
                result_review = await self._complete(session, run, result_review_task, result_review_token, await self.runtime.execute(session, run, challenge, attempt, result_review_task, result_review_token))
                result_review_row = await self._review(session, run, proposal, result_review_task, result_review)
                promoted = await self._apply_result_review(session, run, exec_task, result_review_row)
                next_phase = result_review_row.next_phase if result_review_row.decision == AnalysisDecision.APPROVE.value else "HYPOTHESIS"
                await self._memory(session, run, stage=next_phase, task=result_review_task, working={"last_role": role.value, "last_review": result_review_row.decision, "promoted_fact_ids": promoted, "last_evidence_ids": exec_result.evidence_ids, "candidate_seen": bool(await self._candidate_gate(session, run))})
                if role == AgentRole.VERIFY:
                    candidate = await self._candidate_gate(session, run)
                    call = await session.scalar(select(ToolCall).where(ToolCall.agent_task_id == exec_task.id).order_by(ToolCall.created_at.desc()))
                    artifact = await session.scalar(select(Artifact).where(Artifact.tool_call_id == call.id).order_by(Artifact.created_at.desc())) if call else None
                    reproduced = bool(candidate and artifact and await self._fresh_reproduction(run, candidate, artifact))
                    if candidate and call and artifact and reproduced and exec_result.evidence_ids and result_review_row.decision == AnalysisDecision.APPROVE.value:
                        await self._status(session, run, RunStatus.VERIFYING_FLAG)
                        await deterministic_controller.finalize_verified_candidate(session, run, candidate=candidate.candidate, verify_task_id=exec_task.id, source_artifact_id=artifact.id, producing_tool_call_id=call.id, evidence_ids=exec_result.evidence_ids, pattern_matched=True, fresh_reproduction=True, assistance_level=run.assistance_level or "AUTONOMOUS")
                        await session.commit()
                        return {"status": run.status, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0), "fresh_reproduction": True}
                context = {"replan_reason": "RESULT_REVIEW_CONTINUE", "approved_review": {"proposal_id": proposal.proposal_id, "allowed_tools": proposal.allowed_tools_json, "approved_arguments": plan_review.approved_arguments_json}}
                await self._status(session, run, RunStatus.PLANNING)
            run.recovery_checkpoint_json = {"terminal_reason": "MAX_REPLAN_CYCLES_EXHAUSTED", "cycles": max_cycles, "candidate_gate": "not satisfied"}
            run.last_error_code = "MULTI_AGENT_TERMINAL"
            run.last_error_message = "No candidate satisfied the verification gate after bounded replanning cycles."
            await self._phase(session, run, "REPORTING")
            await self._status(session, run, RunStatus.REPORTING)
            await self._status(session, run, RunStatus.COMPLETED_UNSOLVED)
            await session.commit()
            return {"status": run.status, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0), "terminal_reason": "MAX_REPLAN_CYCLES_EXHAUSTED"}
        except DomainError as error:
            run.last_error_code = error.code
            run.last_error_message = error.message[:4000]
            run.recovery_checkpoint_json = {"terminal_reason": error.code, "details": error.details or {}}
            target = RunStatus.PAUSED_CHECKPOINT if error.code == "MODEL_OUTPUT_SCHEMA_INVALID" else RunStatus.PAUSED_DEPLOYMENT if error.code in {"TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE", "SCRIPT_TARGET_NETWORK_UNAVAILABLE", "TOOL_CATALOG_DRIFT"} else RunStatus.PAUSED_RECOVERY if error.code in {"RUNNER_UNAVAILABLE", "CODEX_STREAM_INTERRUPTED"} else RunStatus.FAILED_ENGINE
            if RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
                await self._status(session, run, target)
            return {"status": run.status, "error_code": error.code, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0)}


multi_agent_orchestrator = MultiAgentOrchestrator()
