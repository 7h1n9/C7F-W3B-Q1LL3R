"""Honest, database-backed acceptance checks for the Phase 2 E2E run."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, AnalysisReview, PlannerProposal, SolutionChainNode
from app.models.run import Artifact, FlagCandidate, FlagProvenance, SolveRun, ToolBatchSummary

FORBIDDEN_SOURCE_MARKERS = ("靶场", "challenge-source", "known-answer", "答案")


async def evaluate_asset_warranty_run(session: AsyncSession, run_id: str) -> dict[str, Any]:
    run = await session.get(SolveRun, run_id)
    if run is None:
        return {"status": "NOT_READY", "reason": "RUN_NOT_FOUND", "output": "ASSET_WARRANTY_MULTI_AGENT_SOLVE=NOT_READY"}
    challenge = await session.get(Challenge, run.challenge_id)
    candidates = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.review_state == "VALID"))).all())
    proven = list((await session.scalars(select(FlagProvenance).where(FlagProvenance.run_id == run.id))).all())
    tasks = list((await session.scalars(select(AgentTask).where(AgentTask.run_id == run.id))).all())
    artifacts = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all())
    proposals = await session.scalar(select(PlannerProposal.id).where(PlannerProposal.run_id == run.id).limit(1))
    reviews = await session.scalar(select(AnalysisReview.proposal_id).where(AnalysisReview.proposal_id == proposals).limit(1)) if proposals else None
    accepted_chain = await session.scalar(select(SolutionChainNode.id).where(SolutionChainNode.run_id == run.id, SolutionChainNode.status == "ACCEPTED").limit(1))
    batches = await session.scalar(select(ToolBatchSummary.id).where(ToolBatchSummary.run_id == run.id).limit(1))
    strings = [run.workspace_path, *(item.file_path for item in artifacts)]
    forbidden_source_audit = not any(marker in value for marker in FORBIDDEN_SOURCE_MARKERS for value in strings)
    challenge_is_asset_warranty = bool(challenge and all(term in f"{challenge.name} {challenge.target_url}".lower() for term in ("asset", "warranty")))
    roles = {task.agent_role for task in tasks}
    autonomous_proof = any(item.source_is_autonomous and item.verification_source_type == "FRESH_REPRODUCTION" for item in proven)
    checks = {
        "asset_warranty_challenge": challenge_is_asset_warranty,
        "codex_sdk": run.engine_type == "codex_sdk",
        "multi_agent_mode": run.solver_mode == "multi_agent_v1",
        "terminal_solved": run.status == "COMPLETED_SOLVED",
        "assistance_allowed": run.assistance_level in {"AUTONOMOUS", "HINT_GUIDED", "EVIDENCE_GUIDED"},
        "planner_analysis_exploit_verify": {"PLANNER", "ANALYSIS", "EXPLOIT", "VERIFY"}.issubset(roles),
        "reviewed_plan": proposals is not None and reviews is not None,
        "accepted_solution_chain": accepted_chain is not None,
        "batched_tool_evidence": batches is not None,
        "fresh_autonomous_provenance": bool(candidates) and autonomous_proof and run.fresh_reproduction_verified,
        "forbidden_source_audit": forbidden_source_audit,
    }
    ready = all(checks.values())
    return {
        "status": "PASS" if ready else "NOT_READY",
        "output": f"ASSET_WARRANTY_MULTI_AGENT_SOLVE={'PASS' if ready else 'NOT_READY'}",
        "checks": checks,
        "run_id": run.id,
        "reason": None if ready else "required E2E evidence is incomplete",
    }
