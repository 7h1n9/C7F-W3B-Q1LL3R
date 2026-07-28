"""Durable, model-backed multi-agent controller.

The controller is intentionally policy-oriented: it creates and leases tasks,
persists model contracts, records evidence, and applies the finish gate.  It
does not invent a fixed GET sequence and it never treats a non-empty evidence
list as an automatic Analysis approval.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AnalysisReview, AgentTask, EvidenceLedger, PlannerProposal
from app.models.run import Artifact, FlagCandidate, RunAttempt, RunExecutionLease, SolveRun, ToolCall
from app.orchestration.role_agent_runtime import RoleAgentRuntime
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskContract,
    AgentTaskKind,
    AgentTaskResultContract,
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

    async def _task(self, session, run: SolveRun, role: AgentRole, kind: AgentTaskKind, objective: str, tools: list[str], *, parent: str | None = None, context: dict | None = None) -> tuple[AgentTask, str]:
        snapshot = await deterministic_controller.memory.read_snapshot(session, run.id)
        policy = await deterministic_controller.permissions.policy(session, role)
        max_calls = min(len(tools), int(policy.max_logical_calls)) if tools else 0
        contract = AgentTaskContract(
            task_id=f"AT-{role.value}-{uuid.uuid4().hex[:12]}", run_id=run.id, agent_role=role, task_kind=kind,
            objective=objective, allowed_tools=tools, created_by_task_id=parent,
            evidence_snapshot_id=snapshot.id if snapshot else None,
            input_snapshot_version=snapshot.version if snapshot else 0,
            budget=TaskBudget(max_logical_calls=max_calls, max_internal_requests=min(8, policy.max_internal_requests), max_runtime_seconds=min(120, policy.max_runtime_seconds)),
            success_condition="produce a validated structured handoff with evidence or an explicit failure classification",
            stop_conditions=["honor task budget", "stop after one discriminating experiment"], context=context or {},
        )
        item = await deterministic_controller.create_task(session, contract)
        token = await deterministic_controller.claim_task(session, item.id, "role-agent-runtime", lease_seconds=contract.budget.max_runtime_seconds)
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
            item = await deterministic_controller.evidence.record(session, EvidenceLedgerContract(
                evidence_id=f"E-{uuid.uuid4().hex[:12]}", run_id=run.id, evidence_type="TOOL_ARTIFACT", artifact_id=artifact.id,
                tool_call_id=call.id, agent_task_id=task.id, summary=(artifact.summary or f"{call.tool_name} result")[:4000], source_chain=[artifact.id, call.id, task.id],
            ))
            evidence_ids.append(item.id)
        return evidence_ids, pairs

    async def _complete(self, session, run: SolveRun, task: AgentTask, token: str, result: AgentTaskResultContract) -> AgentTaskResultContract:
        evidence_ids, pairs = await self._evidence_for_task(session, run, task)
        if evidence_ids:
            result = result.model_copy(update={"evidence_ids": sorted(set((result.evidence_ids or []) + evidence_ids))})
        if pairs and task.agent_role in {AgentRole.RECON.value, AgentRole.EXPLOIT.value} and not result.new_facts:
            result = result.model_copy(update={"new_facts": [{"fact_key": f"{task.agent_role.lower()}:artifact:{pairs[-1][1].id}", "fact_type": "TOOL_OBSERVATION", "value": {"artifact_id": pairs[-1][1].id, "tool": pairs[-1][0].tool_name}, "confidence": 70}]})
        await deterministic_controller.complete_task(session, task.id, result, token)
        await event_service.append(session, run.id, "agent.task.completed", {"task_id": task.id, "agent_role": task.agent_role, "task_kind": task.task_kind, "status": result.status.value, "evidence_ids": result.evidence_ids})
        return result

    async def _proposal(self, session, run: SolveRun, task: AgentTask, result: AgentTaskResultContract) -> PlannerProposal:
        raw = (result.proposed_next_action or {}).get("proposal") or {}
        try:
            contract = PlannerProposalContract.model_validate(raw)
        except Exception:
            # Model-output repair is still driven from durable memory; this is
            # a recovery proposal, not the old fixed RECON/GET controller.
            memory = await deterministic_controller.memory.read_for_role(session, run.id, AgentRole.PLANNER)
            evidence = list(memory.get("evidence_ids") or [])
            next_agent = AgentRole.ANALYSIS if evidence else AgentRole.RECON
            contract = PlannerProposalContract(
                proposal_id=f"PP-{uuid.uuid4().hex[:12]}", run_id=run.id, current_stage=str(memory.get("stage") or "INTAKE"),
                decision_question="Which next bounded action discriminates the active hypothesis?", next_agent=next_agent,
                objective="Review current evidence before selecting a different bounded dimension." if evidence else "Establish one fresh authorized observation.",
                input_fact_ids=list(memory.get("verified_fact_ids") or []), allowed_tools=[] if evidence else ["http_request"],
                success_condition="produce a fresh evidence-backed handoff", stop_conditions=["stop after one result"],
            )
            result.proposed_next_action["runtime_repair"] = "durable_memory_fallback"
        row = PlannerProposal(id=contract.proposal_id, run_id=run.id, proposal_id=contract.proposal_id, current_stage=contract.current_stage, decision_question=contract.decision_question, next_agent=contract.next_agent.value, objective=contract.objective, input_fact_ids_json=contract.input_fact_ids, required_capabilities_json=contract.required_capabilities, allowed_tools_json=contract.allowed_tools, budget_json=contract.budget.model_dump(), success_condition=contract.success_condition, stop_conditions_json=contract.stop_conditions, fallback=contract.fallback, created_by_task_id=task.id)
        session.add(row)
        await session.flush()
        return row

    async def _review(self, session, run: SolveRun, proposal: PlannerProposal, task: AgentTask, result: AgentTaskResultContract) -> AnalysisReview:
        raw = (result.proposed_next_action or {}).get("review") or {}
        try:
            contract = AnalysisReviewContract.model_validate(raw)
        except Exception:
            contract = AnalysisReviewContract(proposal_id=proposal.proposal_id, task_kind=task.task_kind, decision=AnalysisDecision.NEED_MORE_EVIDENCE, confidence=0, question_being_tested=proposal.decision_question, reason="Model review was not schema-valid; evidence is not sufficient for approval.", audit_reason="structured-output-validation-failed")
        proposal_contract = PlannerProposalContract(proposal_id=proposal.proposal_id, run_id=run.id, current_stage=proposal.current_stage, decision_question=proposal.decision_question, next_agent=proposal.next_agent, objective=proposal.objective, input_fact_ids=proposal.input_fact_ids_json, required_capabilities=proposal.required_capabilities_json, allowed_tools=proposal.allowed_tools_json, budget=proposal.budget_json, success_condition=proposal.success_condition, stop_conditions=proposal.stop_conditions_json, fallback=proposal.fallback)
        try:
            deterministic_controller.validate_review(proposal_contract, contract)
        except DomainError as error:
            if contract.decision == AnalysisDecision.APPROVE:
                contract = contract.model_copy(update={"decision": AnalysisDecision.REVISE, "audit_reason": error.code, "reason": error.message})
        row = AnalysisReview(proposal_id=proposal.id, task_kind=contract.task_kind, decision=contract.decision.value, confidence=contract.confidence, question_being_tested=contract.question_being_tested, supporting_evidence_ids_json=contract.supporting_evidence_ids, independent_variable=contract.independent_variable, required_controls_json=contract.required_controls, expected_true_signal_json=contract.expected_true_signal, expected_false_signal_json=contract.expected_false_signal, recommended_tool=contract.recommended_tool, reason=contract.reason, audit_reason=contract.audit_reason, approved_arguments_json=contract.approved_arguments)
        session.add(row)
        await session.flush()
        return row

    async def _memory(self, session, run: SolveRun, *, stage: str, task: AgentTask, working: dict) -> None:
        snapshot = await deterministic_controller.memory.read_snapshot(session, run.id)
        facts = list(snapshot.verified_fact_ids_json or []) if snapshot else []
        evidence = list(snapshot.evidence_ids_json or []) if snapshot else []
        latest = list((await session.scalars(select(EvidenceLedger.id).where(EvidenceLedger.run_id == run.id))).all())
        await deterministic_controller.memory.write_snapshot(session, run.id, stage=stage, working_memory=working, verified_fact_ids=facts, hypothesis_ids=list(snapshot.hypothesis_ids_json or []) if snapshot else [], evidence_ids=sorted(set(evidence + latest)), created_by_task_id=task.id)
        await session.commit()

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
                await self._phase(session, run, "INTAKE" if cycle == 0 else "HYPOTHESIS")
                planner_task, planner_token = await self._task(session, run, AgentRole.PLANNER, AgentTaskKind.PLANNING, "Select the next bounded stage from the current memory snapshot.", [], parent=parent, context=context)
                planner_result = await self.runtime.execute(session, run, challenge, attempt, planner_task, planner_token)
                planner_result = await self._complete(session, run, planner_task, planner_token, planner_result)
                proposal = await self._proposal(session, run, planner_task, planner_result)
                await self._memory(session, run, stage=proposal.current_stage, task=planner_task, working={"proposal": proposal.proposal_id, "decision_question": proposal.decision_question, "approved_review": context.get("approved_review"), "cycle": cycle})
                parent = planner_task.id
                if proposal.next_agent == AgentRole.ANALYSIS:
                    analysis_task, analysis_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.PLAN_REVIEW, proposal.objective, [], parent=planner_task.id, context={"proposal": {"proposal_id": proposal.proposal_id, "current_stage": proposal.current_stage, "decision_question": proposal.decision_question, "next_agent": proposal.next_agent.value, "objective": proposal.objective, "allowed_tools": proposal.allowed_tools_json, "success_condition": proposal.success_condition, "budget": proposal.budget_json}})
                    await self._phase(session, run, "HYPOTHESIS")
                    review_result = await self.runtime.execute(session, run, challenge, attempt, analysis_task, analysis_token)
                    review_result = await self._complete(session, run, analysis_task, analysis_token, review_result)
                    review = await self._review(session, run, proposal, analysis_task, review_result)
                    context = {"approved_review": {"proposal_id": proposal.proposal_id, "allowed_tools": proposal.allowed_tools_json, "approved_arguments": review.approved_arguments_json}, "proposal": proposal.proposal_id} if review.decision == AnalysisDecision.APPROVE.value else {"replan_reason": f"ANALYSIS_{review.decision}"}
                    await self._memory(session, run, stage="HYPOTHESIS", task=analysis_task, working=context | {"review": review.decision})
                    await self._status(session, run, RunStatus.PLANNING)
                    continue
                selected_role = AgentRole(proposal.next_agent)
                if selected_role == AgentRole.VERIFY:
                    candidate = await self._candidate_gate(session, run)
                    if not candidate:
                        context = {"replan_reason": "VERIFY_GATE_REQUIRES_CANDIDATE_ARTIFACT_TOOLCALL"}
                        await self._memory(session, run, stage="FLAG_SEARCH", task=planner_task, working=context)
                        continue
                role = selected_role
                tools = list(proposal.allowed_tools_json or [])
                if role == AgentRole.VERIFY:
                    tools = [tool for tool in tools if tool in {"http_request", "script_run"}] or ["http_request"]
                if not tools and role in {AgentRole.RECON, AgentRole.EXPLOIT, AgentRole.VERIFY}:
                    context = {"replan_reason": "PROPOSAL_HAS_NO_EXECUTABLE_TOOL"}
                    continue
                task_kind = {AgentRole.RECON: AgentTaskKind.RECON, AgentRole.EXPLOIT: AgentTaskKind.EXPLOIT, AgentRole.VERIFY: AgentTaskKind.VERIFY}.get(role, AgentTaskKind.RECON)
                exec_task, exec_token = await self._task(session, run, role, task_kind, proposal.objective, tools, parent=planner_task.id, context={"proposal_id": proposal.proposal_id, "tool": tools[0], "approved_arguments": context.get("approved_review", {}).get("approved_arguments", {})})
                await self._phase(session, run, "BASELINE" if role == AgentRole.RECON else "FLAG_VERIFICATION" if role == AgentRole.VERIFY else "TESTING")
                await self._status(session, run, RunStatus.EXECUTING)
                exec_result = await self.runtime.execute(session, run, challenge, attempt, exec_task, exec_token)
                exec_result = await self._complete(session, run, exec_task, exec_token, exec_result)
                await self._status(session, run, RunStatus.EVALUATING)
                # Every producing stage gets an independent RESULT_REVIEW.
                result_review_task, result_review_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.RESULT_REVIEW, "Review the producing tool result and decide whether it changes the solution chain.", [], parent=exec_task.id, context={"proposal": {"proposal_id": proposal.proposal_id, "current_stage": proposal.current_stage, "decision_question": proposal.decision_question, "next_agent": role.value, "objective": proposal.objective, "allowed_tools": proposal.allowed_tools_json, "success_condition": proposal.success_condition, "budget": proposal.budget_json}, "result": exec_result.proposed_next_action})
                result_review = await self.runtime.execute(session, run, challenge, attempt, result_review_task, result_review_token)
                result_review = await self._complete(session, run, result_review_task, result_review_token, result_review)
                review = await self._review(session, run, proposal, result_review_task, result_review)
                await self._memory(session, run, stage="FLAG_SEARCH" if role != AgentRole.VERIFY else "FLAG_VERIFICATION", task=result_review_task, working={"last_role": role.value, "last_review": review.decision, "last_evidence_ids": result_review.evidence_ids, "candidate_seen": bool(await self._candidate_gate(session, run))})
                if role == AgentRole.VERIFY:
                    candidate = await self._candidate_gate(session, run)
                    call = await session.scalar(select(ToolCall).where(ToolCall.agent_task_id == exec_task.id).order_by(ToolCall.created_at.desc()))
                    artifact = await session.scalar(select(Artifact).where(Artifact.tool_call_id == call.id if call else False).order_by(Artifact.created_at.desc())) if call else None
                    reproduced = bool(candidate and artifact and await self._fresh_reproduction(run, candidate, artifact))
                    if candidate and call and artifact and reproduced and exec_result.evidence_ids:
                        await self._status(session, run, RunStatus.VERIFYING_FLAG)
                        await deterministic_controller.finalize_verified_candidate(session, run, candidate=candidate.candidate, verify_task_id=exec_task.id, source_artifact_id=artifact.id, producing_tool_call_id=call.id, evidence_ids=exec_result.evidence_ids, pattern_matched=True, fresh_reproduction=True, assistance_level=run.assistance_level or "AUTONOMOUS")
                        await session.commit()
                        return {"status": run.status, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0), "fresh_reproduction": True}
                context = {"replan_reason": "NO_VERIFIED_CANDIDATE", "approved_review": {"proposal_id": proposal.proposal_id, "allowed_tools": proposal.allowed_tools_json, "approved_arguments": review.approved_arguments_json} if review.decision == AnalysisDecision.APPROVE.value else {}}
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
            target = RunStatus.PAUSED_DEPLOYMENT if error.code in {"TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE", "SCRIPT_TARGET_NETWORK_UNAVAILABLE", "TOOL_CATALOG_DRIFT"} else RunStatus.PAUSED_RECOVERY if error.code in {"RUNNER_UNAVAILABLE", "CODEX_STREAM_INTERRUPTED"} else RunStatus.FAILED_ENGINE
            if RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
                await self._status(session, run, target)
            return {"status": run.status, "error_code": error.code, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0)}


multi_agent_orchestrator = MultiAgentOrchestrator()
