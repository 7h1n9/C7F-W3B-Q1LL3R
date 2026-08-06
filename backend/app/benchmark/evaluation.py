"""Benchmark metrics and durable run report generation.

The evaluator reads existing durable Run data and never changes execution
state.  It is therefore safe to run after a Run or against a copied snapshot
in an offline thesis-evaluation workflow.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmark.case_definition import BenchmarkCase
from app.models.multi_agent import AgentTask, PlannerProposal
from app.models.run import AgentTurn, RunEvent, SolveRun, ToolCall
from app.models.solver_state import SolverState


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in (value or []) if isinstance(item, Mapping)]


def _status(items: list[Mapping[str, Any]], *statuses: str) -> bool:
    wanted = {status.upper() for status in statuses}
    return any(str(item.get("status") or "").upper() in wanted for item in items)


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "")


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _duration_seconds(snapshot: Mapping[str, Any]) -> float | None:
    if snapshot.get("duration_seconds") is not None:
        return float(snapshot["duration_seconds"])
    started = _timestamp(snapshot.get("started_at"))
    finished = _timestamp(snapshot.get("finished_at"))
    if started and finished:
        return max(0.0, (finished - started).total_seconds())
    return None


def _security_context(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return snapshot.get("security_context") if isinstance(snapshot.get("security_context"), Mapping) else {}


def _evidence_chain(
    context: Mapping[str, Any],
    explicit: Any,
    events: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(explicit, list):
        return [dict(item) for item in explicit if isinstance(item, Mapping)]
    chain: list[dict[str, Any]] = []
    for collection in ("information_evidence", "validation_results", "exploit_results", "impact_assessments", "findings"):
        for item in _items(context.get(collection)):
            chain.append({
                "type": collection,
                "id": item.get("id"),
                "status": item.get("status"),
                "evidence_ids": list(item.get("evidence_ids") or []),
            })
    if chain:
        return chain

    # Current runtime stores the evidence pipeline as durable events. Keep the
    # evaluator read-only and reconstruct a compact, thesis-friendly chain.
    evidence_events = {
        "tool.artifact.created",
        "artifact.created",
        "observation.created",
        "production.result_context.completed",
        "analysis.result_review.dispatched",
        "analysis.result_review.completed",
        "promotion.completed",
    }
    for event in events:
        event_type = _event_type(event)
        if event_type not in evidence_events:
            continue
        chain.append({
            "type": event_type,
            "sequence": event.get("sequence"),
            "id": (
                event.get("artifact_id")
                or event.get("observation_id")
                or event.get("analysis_review_id")
                or event.get("promotion_id")
                or event.get("task_id")
            ),
            "tool": event.get("tool"),
            "evidence_ids": list(event.get("evidence_ids") or []),
        })
    return chain


def evaluate_run(case: BenchmarkCase, snapshot: Mapping[str, Any], output_dir: str | Path | None = None) -> dict[str, Any]:
    """Evaluate one durable Run snapshot and optionally write ``run_report.json``."""
    events = [dict(item) for item in _items(snapshot.get("events"))]
    tasks = _items(snapshot.get("agent_tasks"))
    turns = _items(snapshot.get("agent_turns"))
    tool_calls = _items(snapshot.get("tool_calls"))
    proposals = _items(snapshot.get("planner_proposals"))
    context = _security_context(snapshot)
    hypotheses = _items(context.get("hypotheses"))
    validations = _items(context.get("validation_results"))
    exploits = _items(context.get("exploit_results"))
    impacts = _items(context.get("impact_assessments"))
    findings = _items(context.get("findings"))

    planner_tasks = [task for task in tasks if str(task.get("task_kind") or "").upper() in {"PLANNING", "PLANNER"}]
    planner_calls = len(planner_tasks) or len(proposals) or sum(_event_type(event) == "planner.proposal.created" for event in events)
    agent_calls = len(turns) or len(tasks)
    migration_events = [event for event in events if _event_type(event) == "strategy.migration.applied"]
    feedback_events = [event for event in events if _event_type(event) == "strategy.feedback.created"]
    duplicate_events = [event for event in events if _event_type(event) == "experiment.duplicate_rejected"]
    failed_events = [event for event in events if _event_type(event) in {"tool.failed", "agent.action_rejected"}]
    failed_feedback = sum(
        str(event.get("classification") or "").upper() not in {"", "ORACLE_CONFIRMED"}
        for event in feedback_events
    )
    failed_validation_results = sum(
        str(item.get("status") or "").upper() in {"FAILED", "INCONCLUSIVE"}
        for item in validations
    )
    # Multiple telemetry rows can describe one failed experiment.  Use the
    # strongest available count instead of double-counting the same attempt.
    failed_attempts = max(len(failed_events), failed_feedback, failed_validation_results)
    input_tokens = int(snapshot.get("input_tokens") or sum(int(item.get("input_tokens") or 0) for item in turns))
    output_tokens = int(snapshot.get("output_tokens") or sum(int(item.get("output_tokens") or 0) for item in turns))
    finding_created = _status(findings, "CREATED") or any(
        _event_type(event) == "security.finding.created" for event in events
    )
    discovered = bool(hypotheses or validations or exploits or impacts or findings)
    validation_success = _status(validations, "VALIDATED", "SUCCESS")
    exploit_complete = _status(exploits, "SUCCESS")
    strategy_changes = [*feedback_events, *migration_events]
    explicit_result = snapshot.get("final_result")
    final_result = str(explicit_result) if explicit_result else (
        "SUCCESS" if finding_created else "PARTIAL" if discovered else "INCOMPLETE"
    )

    report = {
        "target": snapshot.get("target") or case.target,
        "vulnerability": snapshot.get("vulnerability") or case.vulnerability_type,
        "case_id": case.case_id,
        "run_id": snapshot.get("run_id"),
        "agents": sorted({str(item.get("agent_role") or item.get("role")) for item in tasks if item.get("agent_role") or item.get("role")}),
        "timeline": events,
        "evidence_chain": _evidence_chain(context, snapshot.get("evidence_chain"), events),
        "strategy_changes": strategy_changes,
        "final_result": final_result,
        # Flat lifecycle flags make thesis aggregation possible without
        # parsing the nested metrics object.
        "validation_success": validation_success,
        "exploit_success": exploit_complete,
        "impact_confirmed": _status(impacts, "CONFIRMED"),
        "finding_created": finding_created,
        "metrics": {
            "agent": {
                "agent_calls": agent_calls,
                "tool_calls": len(tool_calls),
                "planner_calls": planner_calls,
            },
            "effect": {
                "vulnerability_discovered": discovered,
                "validation_success": validation_success,
                "exploit_complete": exploit_complete,
                "finding_created": finding_created,
            },
            "efficiency": {
                "duration_seconds": _duration_seconds(snapshot),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "tool_calls": len(tool_calls),
            },
            "reasoning": {
                "strategy_migrations": len(migration_events),
                "feedback_events": len(feedback_events),
                "failed_attempts": failed_attempts,
                "duplicate_experiments": len(duplicate_events),
            },
        },
    }
    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "run_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return report


async def evaluate_session(
    session: AsyncSession,
    run_id: str,
    case: BenchmarkCase,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize a Run snapshot from existing DB rows and evaluate it."""
    run = await session.get(SolveRun, run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")
    tasks = list((await session.scalars(select(AgentTask).where(AgentTask.run_id == run_id).order_by(AgentTask.created_at))).all())
    turns = list((await session.scalars(select(AgentTurn).where(AgentTurn.run_id == run_id).order_by(AgentTurn.created_at))).all())
    calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at))).all())
    proposals = list((await session.scalars(select(PlannerProposal).where(PlannerProposal.run_id == run_id).order_by(PlannerProposal.created_at))).all())
    events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence))).all())
    state = await session.scalar(select(SolverState).where(SolverState.run_id == run_id))
    snapshot = {
        "run_id": run.id,
        "target": case.target,
        "vulnerability": case.vulnerability_type,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "agent_tasks": [{"id": item.id, "agent_role": item.agent_role, "task_kind": item.task_kind, "status": item.status} for item in tasks],
        "agent_turns": [{"id": item.id, "agent_role": item.agent_role, "input_tokens": item.input_tokens, "output_tokens": item.output_tokens} for item in turns],
        "tool_calls": [{"id": item.id, "tool_name": item.tool_name, "status": item.status} for item in calls],
        "planner_proposals": [{"id": item.id, "current_stage": item.current_stage} for item in proposals],
        "events": [{"sequence": item.sequence, "event_type": item.event_type, **(item.payload_json or {})} for item in events],
        "security_context": (state.security_context_json if state else {}) or {},
        "final_result": "SUCCESS" if run.status == "COMPLETED_SOLVED" else run.status,
    }
    return evaluate_run(case, snapshot, output_dir)
