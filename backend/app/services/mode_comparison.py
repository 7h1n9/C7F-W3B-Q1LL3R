"""Comparable metrics for the single-agent and multi-agent solver modes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.multi_agent import (
    FailureSignature,
    SolutionChainNode,
    VerifiedFact,
)
from app.models.run import (
    Artifact,
    FlagCandidate,
    LogicalToolCall,
    SolveRun,
    ToolBatchSummary,
    ToolRequestFingerprint,
)


async def _count(session: AsyncSession, model: Any, run_id: str, *criteria: Any) -> int:
    query = select(func.count()).select_from(model).where(model.run_id == run_id, *criteria)
    return int(await session.scalar(query) or 0)


async def _metrics(session: AsyncSession, run: SolveRun) -> dict[str, Any]:
    valid_candidates = await _count(session, FlagCandidate, run.id, FlagCandidate.review_state == "VALID")
    first_valid = await session.scalar(
        select(FlagCandidate.created_at)
        .where(FlagCandidate.run_id == run.id, FlagCandidate.review_state == "VALID")
        .order_by(FlagCandidate.created_at)
        .limit(1)
    )
    first_valid_experiment_steps = None
    if first_valid is not None:
        first_valid_experiment_steps = await _count(session, LogicalToolCall, run.id, LogicalToolCall.created_at <= first_valid)
    return {
        "run_id": run.id,
        "solver_mode": run.solver_mode,
        "status": run.status,
        "success": run.status == "COMPLETED_SOLVED",
        "assistance_level": run.assistance_level,
        "agent_steps": run.run_total_agent_steps,
        "logical_tool_calls": run.run_total_logical_tool_calls,
        "tool_call_rows": await _count(session, LogicalToolCall, run.id),
        "duplicate_tool_requests": max(0, await _count(session, ToolRequestFingerprint, run.id) - await _count(session, LogicalToolCall, run.id)),
        "batch_count": await _count(session, ToolBatchSummary, run.id),
        "internal_subrequests": int(await session.scalar(select(func.coalesce(func.sum(ToolBatchSummary.subrequest_count), 0)).where(ToolBatchSummary.run_id == run.id)) or 0),
        "failed_tool_calls": await _count(session, LogicalToolCall, run.id, LogicalToolCall.status.in_(["FAILED", "ERROR"])),
        "failure_signatures": await _count(session, FailureSignature, run.id),
        "rejected_or_reviewed_paths": await _count(session, SolutionChainNode, run.id, SolutionChainNode.status.in_(["REJECTED", "REVIEWED"])),
        "verified_facts": await _count(session, VerifiedFact, run.id, VerifiedFact.promotion_status == "PROMOTED"),
        "solution_chain_nodes": await _count(session, SolutionChainNode, run.id, SolutionChainNode.status == "ACCEPTED"),
        "evidence_artifacts": await _count(session, Artifact, run.id, Artifact.retention_class.in_(["PROTECTED", "FINAL", "FRESH_REPRODUCTION"])),
        "valid_flag_candidates": valid_candidates,
        "first_valid_experiment_steps": first_valid_experiment_steps,
        "fresh_reproduction_verified": bool(run.fresh_reproduction_verified),
        "terminal_cleanup_completed": bool(run.terminal_cleanup_completed),
        "runtime_seconds": (run.finished_at - run.started_at).total_seconds() if run.finished_at and run.started_at else None,
    }


def _delta(single: dict[str, Any], multi: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in multi.items():
        if key in {"run_id", "solver_mode", "status", "assistance_level"}:
            continue
        other = single.get(key)
        if isinstance(value, (int, float)) and isinstance(other, (int, float)):
            result[key] = value - other
    return result


async def compare_runs(session: AsyncSession, single_run_id: str, multi_run_id: str) -> dict[str, Any]:
    single = await session.get(SolveRun, single_run_id)
    multi = await session.get(SolveRun, multi_run_id)
    if single is None or multi is None:
        missing = single_run_id if single is None else multi_run_id
        raise ValueError(f"run not found: {missing}")
    single_metrics = await _metrics(session, single)
    multi_metrics = await _metrics(session, multi)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "single_agent": single_metrics,
        "multi_agent_v1": multi_metrics,
        "delta_multi_minus_single": _delta(single_metrics, multi_metrics),
        "success_comparison": {
            "single_agent": single_metrics["success"],
            "multi_agent_v1": multi_metrics["success"],
            "winner": "multi_agent_v1" if multi_metrics["success"] and not single_metrics["success"] else "single_agent" if single_metrics["success"] and not multi_metrics["success"] else "tie",
        },
    }
