from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.models.multi_agent import AgentTask, ApprovedAction
from app.models.run import RunAttempt, RunExecutionLease, SolveRun, ToolCall
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.multi_agent import AgentTaskStatus
from app.services.events import event_service


async def cancel_run(session, run_id: str, reason: str = "Run cancelled by user.") -> SolveRun:
    """Atomically close a Run and all durable execution resources."""
    run = await session.get(SolveRun, run_id, with_for_update=True)
    if run is None:
        raise ValueError("RUN_NOT_FOUND")
    now = datetime.now(UTC)
    if RunStatus(run.status) != RunStatus.CANCELLED:
        transition(run, RunStatus.CANCELLED)
    run.status = RunStatus.CANCELLED.value
    run.finished_at = run.finished_at or now
    attempts = list((await session.scalars(select(RunAttempt).where(
        RunAttempt.run_id == run_id, RunAttempt.finished_at.is_(None)
    ))).all())
    for attempt in attempts:
        attempt.status = RunStatus.CANCELLED.value
        attempt.finished_at = now
        attempt.error_code = "RUN_CANCELLED"
    tasks = list((await session.scalars(select(AgentTask).where(
        AgentTask.run_id == run_id,
        AgentTask.status == AgentTaskStatus.RUNNING.value,
    ))).all())
    for task in tasks:
        task.status = AgentTaskStatus.INTERRUPTED.value
        task.cancel_requested = True
        task.lease_expires_at = None
    actions = list((await session.scalars(select(ApprovedAction).where(
        ApprovedAction.run_id == run_id, ApprovedAction.status == "ACTIVE"
    ))).all())
    for action in actions:
        action.status = "REJECTED"
        action.compile_error_json = {"code": "RUN_CANCELLED", "message": reason[:1000]}
    await session.execute(delete(RunExecutionLease).where(RunExecutionLease.run_id == run_id))
    await session.execute(
        ToolCall.__table__.update()
        .where(ToolCall.run_id == run_id, ToolCall.status.in_(["REQUESTED", "STARTED"]))
        .values(status="CANCELLED", finished_at=now)
    )
    await session.commit()
    await event_service.append(session, run.id, "run.cancelled", {
        "reason": reason[:4000],
        "attempts_closed": len(attempts),
        "tasks_interrupted": len(tasks),
        "actions_rejected": len(actions),
        "lease_deleted": True,
    })
    return run
