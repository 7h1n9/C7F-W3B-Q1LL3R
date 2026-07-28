"""Inspectable API for the opt-in multi-agent core loop."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import (
    AgentRolePolicy,
    AgentTask,
    AnalysisReview,
    EvidenceLedger,
    PlannerProposal,
)
from app.models.run import SolveRun, WebResearchRecord
from app.schemas.multi_agent import (
    AgentTaskContract,
    AgentTaskResultContract,
    AnalysisReviewContract,
    CandidateVerificationContract,
    EvidenceLedgerContract,
    PlannerProposalContract,
    SolutionChainNodeContract,
)
from app.schemas.web_research import WebResearchPromotion, WebResearchRequest
from app.services.acceptance import evaluate_asset_warranty_run
from app.services.mode_comparison import compare_runs
from app.services.multi_agent import deterministic_controller
from app.services.web_research import web_research_service

router = APIRouter(prefix="/multi-agent", tags=["multi-agent"])


async def _run(session: AsyncSession, run_id: str) -> SolveRun:
    run = await session.get(SolveRun, run_id)
    if run is None:
        raise DomainError("RUN_NOT_FOUND", "Solve run not found.", status_code=404)
    if run.solver_mode != "multi_agent_v1":
        raise DomainError("MULTI_AGENT_NOT_ENABLED", "This run is using the single-agent compatibility mode.", {"solver_mode": run.solver_mode}, status_code=409)
    return run


@router.get("/policies")
async def list_policies(session: AsyncSession = Depends(get_session)) -> dict:
    await deterministic_controller.seed_policies(session)
    await session.commit()
    items = list((await session.scalars(select(AgentRolePolicy).order_by(AgentRolePolicy.role))).all())
    return {"data": [{"role": item.role, "allowed_tools": item.allowed_tools_json, "allowed_outputs": item.allowed_outputs_json, "forbidden_operations": item.forbidden_operations_json, "budget": {"max_logical_calls": item.max_logical_calls, "max_internal_requests": item.max_internal_requests, "max_runtime_seconds": item.max_runtime_seconds}} for item in items]}


@router.post("/runs/{run_id}/tasks", status_code=201)
async def create_task(run_id: str, payload: AgentTaskContract, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    if payload.run_id != run_id:
        raise DomainError("AGENT_TASK_RUN_MISMATCH", "Task run_id does not match the URL.")
    await deterministic_controller.seed_policies(session)
    task = await deterministic_controller.create_task(session, payload)
    await session.commit()
    return {"data": {"task_id": task.id, "status": task.status, "agent_role": task.agent_role, "budget": task.budget_json}}


@router.post("/tasks/{task_id}/claim")
async def claim_task(task_id: str, payload: dict | None = None, session: AsyncSession = Depends(get_session)) -> dict:
    task = await session.get(AgentTask, task_id)
    if task is None:
        raise DomainError("AGENT_TASK_NOT_FOUND", "Agent task does not exist.", status_code=404)
    await _run(session, task.run_id)
    body = payload or {}
    token = await deterministic_controller.claim_task(session, task_id, str(body.get("owner") or "controller"), int(body.get("lease_seconds") or task.timeout_seconds))
    await session.commit()
    return {"data": {"task_id": task_id, "lease_token": token, "status": "RUNNING"}}


@router.post("/tasks/{task_id}/result")
async def complete_task(task_id: str, payload: AgentTaskResultContract, session: AsyncSession = Depends(get_session)) -> dict:
    task = await session.get(AgentTask, task_id)
    if task is None:
        raise DomainError("AGENT_TASK_NOT_FOUND", "Agent task does not exist.", status_code=404)
    await _run(session, task.run_id)
    decision = await deterministic_controller.complete_task(session, task_id, payload, payload.lease_token)
    await session.commit()
    return {"data": {"task_id": task_id, "status": task.status, "promotion": decision.model_dump()}}


@router.get("/runs/{run_id}/tasks")
async def list_tasks(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    items = list((await session.scalars(select(AgentTask).where(AgentTask.run_id == run_id).order_by(AgentTask.created_at))).all())
    return {"data": [{"task_id": item.id, "role": item.agent_role, "status": item.status, "objective": item.objective, "retry_count": item.retry_count, "optimistic_version": item.optimistic_version} for item in items]}


@router.post("/runs/{run_id}/evidence", status_code=201)
async def record_evidence(run_id: str, payload: EvidenceLedgerContract, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    if payload.run_id != run_id:
        raise DomainError("EVIDENCE_RUN_MISMATCH", "Evidence run_id does not match the URL.")
    item = await deterministic_controller.evidence.record(session, payload)
    await session.commit()
    return {"data": {"evidence_id": item.id, "status": item.status, "source_chain": item.source_chain}}


@router.get("/runs/{run_id}/evidence")
async def list_evidence(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    items = list((await session.scalars(select(EvidenceLedger).where(EvidenceLedger.run_id == run_id).order_by(EvidenceLedger.created_at))).all())
    return {"data": [{"evidence_id": item.id, "type": item.evidence_type, "summary": item.summary, "artifact_id": item.artifact_id, "tool_call_id": item.tool_call_id, "agent_task_id": item.agent_task_id, "status": item.status} for item in items]}


@router.post("/runs/{run_id}/proposals", status_code=201)
async def create_proposal(run_id: str, payload: PlannerProposalContract, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    if payload.run_id != run_id:
        raise DomainError("PROPOSAL_RUN_MISMATCH", "Proposal run_id does not match the URL.")
    await deterministic_controller.seed_policies(session)
    if payload.next_agent.value == "PLANNER" or "sqlmap_run" in payload.allowed_tools:
        raise DomainError("PLANNER_TOOL_FORBIDDEN", "Planner proposals cannot directly schedule forbidden execution.")
    item = PlannerProposal(id=payload.proposal_id, run_id=run_id, proposal_id=payload.proposal_id, current_stage=payload.current_stage, next_agent=payload.next_agent.value, objective=payload.objective, input_fact_ids_json=payload.input_fact_ids, required_capabilities_json=payload.required_capabilities, allowed_tools_json=payload.allowed_tools, budget_json=payload.budget.model_dump(), success_condition=payload.success_condition, stop_conditions_json=payload.stop_conditions, fallback=payload.fallback)
    session.add(item)
    await session.commit()
    return {"data": {"proposal_id": item.proposal_id, "status": item.status}}


@router.post("/runs/{run_id}/proposals/{proposal_id}/review")
async def review_proposal(run_id: str, proposal_id: str, payload: AnalysisReviewContract, session: AsyncSession = Depends(get_session)) -> dict:
    run = await _run(session, run_id)
    proposal = await session.scalar(select(PlannerProposal).where(PlannerProposal.run_id == run.id, PlannerProposal.proposal_id == proposal_id))
    if proposal is None:
        raise DomainError("PROPOSAL_NOT_FOUND", "Planner proposal does not exist.", status_code=404)
    proposal_contract = PlannerProposalContract(proposal_id=proposal.proposal_id, run_id=run_id, current_stage=proposal.current_stage, next_agent=proposal.next_agent, objective=proposal.objective, input_fact_ids=proposal.input_fact_ids_json, required_capabilities=proposal.required_capabilities_json, allowed_tools=proposal.allowed_tools_json, budget=proposal.budget_json, success_condition=proposal.success_condition, stop_conditions=proposal.stop_conditions_json, fallback=proposal.fallback)
    deterministic_controller.validate_review(proposal_contract, payload)
    review = AnalysisReview(proposal_id=proposal.id, decision=payload.decision.value, confidence=payload.confidence, question_being_tested=payload.question_being_tested, supporting_evidence_ids_json=payload.supporting_evidence_ids, independent_variable=payload.independent_variable, required_controls_json=payload.required_controls, expected_true_signal_json=payload.expected_true_signal, expected_false_signal_json=payload.expected_false_signal, recommended_tool=payload.recommended_tool, reason=payload.reason, audit_reason=payload.audit_reason)
    proposal.status = "APPROVED" if payload.decision.value == "APPROVE" else "REVIEWED"
    session.add(review)
    await session.commit()
    return {"data": {"proposal_id": proposal_id, "decision": payload.decision.value, "status": proposal.status}}


@router.post("/runs/{run_id}/solution-chain", status_code=201)
async def accept_solution_node(run_id: str, payload: SolutionChainNodeContract, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    if payload.run_id != run_id:
        raise DomainError("SOLUTION_NODE_RUN_MISMATCH", "Solution node run_id does not match the URL.")
    item = await deterministic_controller.solution_chain.accept(session, payload)
    await session.commit()
    return {"data": {"node_id": item.node_id, "status": item.status, "capability_added": item.capability_added}}


@router.post("/runs/{run_id}/verify-candidate")
async def verify_candidate(run_id: str, payload: CandidateVerificationContract, session: AsyncSession = Depends(get_session)) -> dict:
    run = await _run(session, run_id)
    item = await deterministic_controller.finalize_verified_candidate(session, run, **payload.model_dump())
    await session.commit()
    return {"data": {"candidate_id": item.id, "candidate": item.candidate, "status": run.status, "fresh_reproduction": run.fresh_reproduction_verified}}


@router.post("/runs/{run_id}/web-research")
async def web_research(run_id: str, payload: WebResearchRequest, session: AsyncSession = Depends(get_session)) -> dict:
    run = await _run(session, run_id)
    task = await session.get(AgentTask, payload.task_id) if payload.task_id else None
    if task is not None and task.run_id != run.id:
        raise DomainError("WEB_RESEARCH_TASK_MISMATCH", "Web Research task does not belong to this Run.")
    challenge = await session.get(Challenge, run.challenge_id)
    result = await web_research_service.search(session, run, task, payload.query, requested_by=payload.requested_by, challenge=challenge)
    await session.commit()
    return {"data": result}


@router.post("/runs/{run_id}/web-research/promote")
async def promote_web_research(run_id: str, payload: WebResearchPromotion, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    record = await session.get(WebResearchRecord, payload.record_id)
    if record is None or record.run_id != run_id:
        raise DomainError("WEB_RESEARCH_RUN_MISMATCH", "Web Research record does not belong to this Run.")
    item = await web_research_service.promote(session, payload.record_id, payload.fact_ids)
    await session.commit()
    return {"data": {"record_id": item.id, "status": item.status, "used_in_fact_ids": item.used_in_fact_ids_json}}


@router.get("/runs/{run_id}/web-research")
async def list_web_research(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    items = list((await session.scalars(select(WebResearchRecord).where(WebResearchRecord.run_id == run_id).order_by(WebResearchRecord.created_at))).all())
    return {"data": [{"record_id": item.id, "query": item.query, "risk_level": item.risk_level, "answer_leak_risk": item.answer_leak_risk, "status": item.status, "source_urls": item.source_urls_json, "summary": item.summary} for item in items]}


@router.get("/compare")
async def compare_solver_modes(single_run_id: str, multi_run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": await compare_runs(session, single_run_id, multi_run_id)}


@router.get("/runs/{run_id}/acceptance")
async def multi_agent_acceptance(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await _run(session, run_id)
    return {"data": await evaluate_asset_warranty_run(session, run_id)}
