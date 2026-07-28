"""Controller-owned multi-agent Run Loop.

Agents only construct contracts.  This orchestrator creates/leases tasks,
persists proposals and reviews, invokes approved tools, and owns lifecycle
transitions.  It intentionally does not persist agent transcripts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AnalysisReview, AgentTask, EvidenceLedger, PlannerProposal, SolutionChainNode, VerifiedFact
from app.models.run import Artifact, FlagCandidate, RunAttempt, RunExecutionLease, SolveRun, ToolCall
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskContract,
    AgentTaskResultContract,
    AgentTaskStatus,
    AnalysisDecision,
    AnalysisReviewContract,
    EvidenceLedgerContract,
    PlannerProposalContract,
    SolutionChainNodeContract,
    TaskBudget,
)
from app.services.events import event_service
from app.services.multi_agent import deterministic_controller
from app.services.solver_state import solver_state_service
from app.services.flags import flag_service
from app.tools.gateway import tool_gateway


class MultiAgentOrchestrator:
    def __init__(self, tool_invoker=None) -> None:
        self.tool_invoker = tool_invoker or tool_gateway.invoke

    async def _status(self, session, run: SolveRun, target: RunStatus) -> None:
        if RunStatus(run.status) == target:
            return
        transition(run, target)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status, "controller": "multi_agent_v1"})

    async def _phase(self, session, run: SolveRun, phase: str) -> None:
        """Persist the solver phase independently from lifecycle status."""
        if str(run.current_phase or "") == phase:
            return
        previous = run.current_phase
        run.current_phase = phase
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "run.phase_changed",
            {"previous_phase": previous, "phase": phase, "source": "multi_agent_controller"},
        )

    async def _task(self, session, run: SolveRun, role: AgentRole, objective: str, tools: list[str], *, parent: str | None = None) -> tuple[AgentTask, str]:
        item = await deterministic_controller.create_task(
            session,
            AgentTaskContract(
                task_id=f"AT-{role.value}-{uuid.uuid4().hex[:12]}",
                run_id=run.id,
                agent_role=role,
                objective=objective,
                allowed_tools=tools,
                created_by_task_id=parent,
                budget=TaskBudget(max_logical_calls=max(1, len(tools)), max_internal_requests=8, max_runtime_seconds=120),
                success_condition="produce a structured, evidence-backed handoff",
            ),
        )
        token = await deterministic_controller.claim_task(session, item.id, "multi-agent-controller")
        return item, token

    async def _planner(self, session, run: SolveRun) -> tuple[PlannerProposal, AgentTask, str]:
        task, token = await self._task(session, run, AgentRole.PLANNER, "Select the next bounded stage from the current memory snapshot.", [])
        contract = PlannerProposalContract(
            proposal_id=f"PP-{uuid.uuid4().hex[:12]}", run_id=run.id, current_stage="INTAKE",
            next_agent=AgentRole.RECON, objective="Establish the authorized baseline and entry surface.",
            allowed_tools=["http_request"], success_condition="one fresh baseline observation",
            budget=TaskBudget(max_logical_calls=1, max_internal_requests=8, max_runtime_seconds=120),
        )
        row = PlannerProposal(id=contract.proposal_id, run_id=run.id, proposal_id=contract.proposal_id, current_stage=contract.current_stage, next_agent=contract.next_agent.value, objective=contract.objective, input_fact_ids_json=contract.input_fact_ids, required_capabilities_json=contract.required_capabilities, allowed_tools_json=contract.allowed_tools, budget_json=contract.budget.model_dump(), success_condition=contract.success_condition, stop_conditions_json=contract.stop_conditions, fallback=contract.fallback, created_by_task_id=task.id)
        session.add(row)
        await session.flush()
        await deterministic_controller.complete_task(session, task.id, AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"next_agent": contract.next_agent.value, "proposal_id": contract.proposal_id}, handoff_summary="Planner proposal persisted."), token)
        await event_service.append(session, run.id, "planner.proposal.created", {"proposal_id": row.proposal_id, "next_agent": row.next_agent})
        return row, task, token

    async def _analysis(self, session, run: SolveRun, proposal: PlannerProposal, evidence_ids: list[str]) -> tuple[AnalysisReview, AgentTask, str]:
        task, token = await self._task(session, run, AgentRole.ANALYSIS, "Review controls and evidence for the planner proposal.", [], parent=proposal.created_by_task_id)
        review_contract = AnalysisReviewContract(
            proposal_id=proposal.proposal_id,
            decision=AnalysisDecision.APPROVE if evidence_ids else AnalysisDecision.NEED_MORE_EVIDENCE,
            confidence=90 if evidence_ids else 20,
            question_being_tested="Does the authorized target expose a reproducible entry surface?",
            supporting_evidence_ids=evidence_ids,
            independent_variable="request path",
            required_controls={"authorized_host": "challenge.allowed_hosts"},
            reason="Evidence is sufficient for the bounded next action." if evidence_ids else "Baseline evidence is still required.",
        )
        deterministic_controller.validate_review(
            PlannerProposalContract.model_validate({"proposal_id": proposal.proposal_id, "run_id": run.id, "current_stage": proposal.current_stage, "next_agent": proposal.next_agent, "objective": proposal.objective, "input_fact_ids": proposal.input_fact_ids_json, "required_capabilities": proposal.required_capabilities_json, "allowed_tools": proposal.allowed_tools_json, "budget": proposal.budget_json, "success_condition": proposal.success_condition, "stop_conditions": proposal.stop_conditions_json, "fallback": proposal.fallback}),
            review_contract,
        ) if evidence_ids else None
        review = AnalysisReview(proposal_id=proposal.id, decision=review_contract.decision.value, confidence=review_contract.confidence, question_being_tested=review_contract.question_being_tested, supporting_evidence_ids_json=review_contract.supporting_evidence_ids, independent_variable=review_contract.independent_variable, required_controls_json=review_contract.required_controls, expected_true_signal_json=review_contract.expected_true_signal, expected_false_signal_json=review_contract.expected_false_signal, recommended_tool=review_contract.recommended_tool, reason=review_contract.reason, audit_reason="controller review")
        session.add(review)
        await session.flush()
        await deterministic_controller.complete_task(session, task.id, AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, evidence_ids=evidence_ids, handoff_summary=f"Analysis decision: {review.decision}."), token)
        await event_service.append(session, run.id, "analysis.review.created", {"proposal_id": proposal.proposal_id, "decision": review.decision})
        return review, task, token

    async def _stage_result(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, *, final_verification: bool = False) -> tuple[dict, ToolCall | None, Artifact | None]:
        """Invoke one approved stage and resolve its own ToolCall/Artifact pair."""
        result = await self.tool_invoker(
            session,
            run,
            challenge,
            "http_request",
            {"url": challenge.target_url, "method": "GET", "final_verification": final_verification},
            execution_layer="multi_agent",
            logical_tool_call_id=f"mcp:{run.id}:{attempt.id}:multi-agent:{task.id}",
        )
        call = await session.scalar(
            select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.logical_tool_call_id == f"mcp:{run.id}:{attempt.id}:multi-agent:{task.id}")
        )
        artifact = await session.scalar(
            select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == (call.id if call else "")).order_by(Artifact.created_at.desc())
        )
        return result, call, artifact

    async def _evidence(self, session, run: SolveRun, task: AgentTask, call: ToolCall | None, artifact: Artifact | None, summary: str) -> str | None:
        if not call or not artifact:
            return None
        item = await deterministic_controller.evidence.record(
            session,
            EvidenceLedgerContract(
                evidence_id=f"E-{uuid.uuid4().hex[:12]}",
                run_id=run.id,
                evidence_type="HTTP_RESPONSE",
                artifact_id=artifact.id,
                tool_call_id=call.id,
                agent_task_id=task.id,
                summary=summary,
                source_chain=[artifact.id, call.id, task.id],
            ),
        )
        return item.id

    async def run(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, lease: RunExecutionLease) -> dict:
        await deterministic_controller.seed_policies(session)
        await solver_state_service.initialize(session, run, challenge.challenge_type, [], challenge.name, challenge.description)
        proposal, planner_task, _ = await self._planner(session, run)
        await deterministic_controller.memory.write_snapshot(session, run.id, stage="INTAKE", working_memory={"proposal_id": proposal.proposal_id, "target": challenge.target_url}, verified_fact_ids=[], hypothesis_ids=[], evidence_ids=[], created_by_task_id=planner_task.id)
        run.run_total_agent_steps += 1
        run.attempt_agent_steps += 1
        evidence_ids: list[str] = []
        try:
            if RunStatus(run.status) in {RunStatus.PAUSED_DEPLOYMENT, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RATE_LIMIT, RunStatus.WAITING_USER, RunStatus.WAITING_CONFIGURATION}:
                await self._status(session, run, RunStatus.PLANNING)
            else:
                await self._status(session, run, RunStatus.PREPARING)
                await self._status(session, run, RunStatus.ANALYZING)
                await self._status(session, run, RunStatus.PLANNING)

            recon_task, recon_token = await self._task(session, run, AgentRole.RECON, proposal.objective, ["http_request"], parent=planner_task.id)
            await self._phase(session, run, "BASELINE")
            await self._status(session, run, RunStatus.EXECUTING)
            recon_result, recon_call, recon_artifact = await self._stage_result(session, run, challenge, attempt, recon_task)
            if recon_call and recon_artifact and str(recon_result.get("status")) == "COMPLETED":
                evidence_id = await self._evidence(session, run, recon_task, recon_call, recon_artifact, "Fresh authorized baseline response")
                if evidence_id:
                    evidence_ids.append(evidence_id)
                promotion = await deterministic_controller.complete_task(
                    session,
                    recon_task.id,
                    AgentTaskResultContract(task_id=recon_task.id, status=AgentTaskStatus.COMPLETED, evidence_ids=evidence_ids, new_facts=[{"fact_key": "baseline:authorized_target", "fact_type": "HTTP_ENDPOINT", "value": {"url": challenge.target_url}, "confidence": 90}], handoff_summary="Baseline response recorded."),
                    recon_token,
                )
                facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.source_task_id == recon_task.id))).all())
                if facts and evidence_ids:
                    await deterministic_controller.solution_chain.accept(session, SolutionChainNodeContract(node_id=f"SC-{uuid.uuid4().hex[:12]}", run_id=run.id, stage="BASELINE", objective=proposal.objective, agent_task_id=recon_task.id, result_fact_ids=[facts[0].id], capability_added="authorized_baseline", evidence_ids=evidence_ids))
                await deterministic_controller.memory.write_snapshot(session, run.id, stage="BASELINE", working_memory={"baseline": "recorded", "promotion": promotion.status.value}, verified_fact_ids=[item.id for item in facts], hypothesis_ids=[], evidence_ids=evidence_ids, created_by_task_id=recon_task.id)
            else:
                await deterministic_controller.complete_task(session, recon_task.id, AgentTaskResultContract(task_id=recon_task.id, status=AgentTaskStatus.BLOCKED, failure_classification={"fingerprint": "baseline-runner", "classification": "RUNNER_FAILURE", "retryable": True, "reason": str(recon_result.get("error") or recon_result.get("summary") or "baseline failed"), "next_allowed_condition": "runner available"}, handoff_summary="Baseline could not be executed."), recon_token)
            await self._phase(session, run, "HYPOTHESIS")
            await self._status(session, run, RunStatus.EVALUATING)
            review, analysis_task, _ = await self._analysis(session, run, proposal, evidence_ids)
            run.run_total_agent_steps += 2
            run.attempt_agent_steps += 2
            if review.decision != AnalysisDecision.APPROVE.value:
                await self._status(session, run, RunStatus.REPORTING)
                await self._status(session, run, RunStatus.COMPLETED_UNSOLVED)
                return {"status": run.status, "agent_tasks": 3, "evidence_ids": evidence_ids}

            exploit_task, exploit_token = await self._task(session, run, AgentRole.EXPLOIT, "Execute only the approved bounded request and preserve its artifact.", ["http_request"], parent=analysis_task.id)
            await self._phase(session, run, "TESTING")
            await self._status(session, run, RunStatus.PLANNING)
            await self._status(session, run, RunStatus.EXECUTING)
            exploit_result, exploit_call, exploit_artifact = await self._stage_result(session, run, challenge, attempt, exploit_task)
            exploit_evidence = await self._evidence(session, run, exploit_task, exploit_call, exploit_artifact, "Analysis-approved bounded execution result") if exploit_result.get("status") == "COMPLETED" else None
            if exploit_evidence:
                evidence_ids.append(exploit_evidence)
            await deterministic_controller.complete_task(session, exploit_task.id, AgentTaskResultContract(task_id=exploit_task.id, status=AgentTaskStatus.COMPLETED if exploit_result.get("status") == "COMPLETED" else AgentTaskStatus.BLOCKED, evidence_ids=[exploit_evidence] if exploit_evidence else [], handoff_summary="Approved bounded exploit stage completed." if exploit_result.get("status") == "COMPLETED" else "Approved stage blocked.", failure_classification=None if exploit_result.get("status") == "COMPLETED" else {"fingerprint": "approved-exploit-failed", "classification": "RUNNER_FAILURE", "retryable": True, "reason": str(exploit_result.get("error") or "exploit stage failed"), "next_allowed_condition": "runner available"}), exploit_token)

            verify_task, verify_token = await self._task(session, run, AgentRole.VERIFY, "Independently reproduce the structured candidate with a fresh request.", ["http_request"], parent=exploit_task.id)
            await self._phase(session, run, "FLAG_VERIFICATION")
            await self._status(session, run, RunStatus.EVALUATING)
            verify_result, verify_call, verify_artifact = await self._stage_result(session, run, challenge, attempt, verify_task, final_verification=True)
            verify_evidence = await self._evidence(session, run, verify_task, verify_call, verify_artifact, "Independent fresh reproduction response") if verify_result.get("status") == "COMPLETED" else None
            if verify_evidence:
                evidence_ids.append(verify_evidence)
            candidate = await session.scalar(select(FlagCandidate).where(FlagCandidate.run_id == run.id).order_by(FlagCandidate.created_at.desc()))
            reproduced = bool(candidate and verify_artifact and Path(run.workspace_path, verify_artifact.file_path).is_file() and candidate.candidate in Path(run.workspace_path, verify_artifact.file_path).read_text(encoding="utf-8", errors="replace"))
            await deterministic_controller.complete_task(session, verify_task.id, AgentTaskResultContract(task_id=verify_task.id, status=AgentTaskStatus.COMPLETED if verify_result.get("status") == "COMPLETED" else AgentTaskStatus.BLOCKED, evidence_ids=[verify_evidence] if verify_evidence else [], handoff_summary="Fresh reproduction completed." if reproduced else "Fresh reproduction did not reproduce a structured candidate.", failure_classification=None if verify_result.get("status") == "COMPLETED" else {"fingerprint": "fresh-reproduction-failed", "classification": "RUNNER_FAILURE", "retryable": True, "reason": str(verify_result.get("error") or "verification request failed"), "next_allowed_condition": "runner available"}), verify_token)
            if candidate and reproduced and verify_evidence and verify_artifact and verify_call:
                await self._status(session, run, RunStatus.VERIFYING_FLAG)
                await deterministic_controller.finalize_verified_candidate(session, run, candidate=candidate.candidate, verify_task_id=verify_task.id, source_artifact_id=verify_artifact.id, producing_tool_call_id=verify_call.id, evidence_ids=[verify_evidence], pattern_matched=True, fresh_reproduction=True, assistance_level=run.assistance_level or "AUTONOMOUS")
                await session.commit()
                return {"status": run.status, "agent_tasks": 5, "evidence_ids": evidence_ids, "fresh_reproduction": True}
            await self._phase(session, run, "REPORTING")
            await self._status(session, run, RunStatus.REPORTING)
            await self._status(session, run, RunStatus.COMPLETED_UNSOLVED)
            run.run_total_agent_steps += 2
            run.attempt_agent_steps += 2
            return {"status": run.status, "agent_tasks": 5, "evidence_ids": evidence_ids, "fresh_reproduction": False}
        except DomainError as error:
            run.last_error_code = error.code
            run.last_error_message = error.message[:4000]
            target = RunStatus.PAUSED_DEPLOYMENT if error.code in {"TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE", "SCRIPT_TARGET_NETWORK_UNAVAILABLE", "TOOL_CATALOG_DRIFT"} else RunStatus.PAUSED_RECOVERY if error.code in {"RUNNER_UNAVAILABLE", "CODEX_STREAM_INTERRUPTED"} else RunStatus.FAILED_ENGINE
            if RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
                if target not in {RunStatus.PAUSED_DEPLOYMENT, RunStatus.PAUSED_RECOVERY} or RunStatus(run.status) != target:
                    try:
                        await self._status(session, run, target)
                    except DomainError:
                        run.status = target.value
                        await session.commit()
            return {"status": run.status, "error_code": error.code, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0)}


multi_agent_orchestrator = MultiAgentOrchestrator()
