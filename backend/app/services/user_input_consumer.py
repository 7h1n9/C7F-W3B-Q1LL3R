"""Durable user-input consumption shared by API, Supervisor and Agent runs."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.run import RunAttempt, RunUserInput, SolveRun
from app.models.solver_state import SolverState
from app.services.events import event_service


async def consume_user_inputs(session, run: SolveRun, attempt: RunAttempt | None = None) -> dict:
    queued = list((await session.scalars(select(RunUserInput).where(
        RunUserInput.run_id == run.id,
        RunUserInput.status == "QUEUED",
        RunUserInput.consumed_at.is_(None),
    ).order_by(RunUserInput.revision, RunUserInput.created_at).with_for_update())).all())
    if not queued:
        return {"items": [], "input_ids": [], "user_inputs": [], "text": ""}

    now = datetime.now(UTC)
    user_inputs = [{
        "id": item.id,
        "revision": item.revision,
        "content": item.content,
        "input_type": item.input_type,
        "created_at": item.created_at.isoformat(),
    } for item in queued]
    for item in queued:
        item.status = "CONSUMED"
        item.consumed_at = now
        item.consumed_by_attempt_id = attempt.id if attempt else None

    hints = dict(run.hints_json or {})
    hints["user_inputs"] = [*(hints.get("user_inputs") or []), *user_inputs][-100:]
    run.hints_json = hints
    checkpoint = dict(run.recovery_checkpoint_json or {})
    checkpoint["last_user_input_ids"] = [item["id"] for item in user_inputs]
    checkpoint["last_user_input_revisions"] = [item["revision"] for item in user_inputs]
    counters = dict(checkpoint.get("supervisor_counters") or {})
    counters["no_progress_count"] = 0
    counters["same_error_count"] = 0
    checkpoint["supervisor_counters"] = counters
    run.recovery_checkpoint_json = checkpoint
    state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
    if state is not None:
        decision_card = dict(state.last_decision_card_json or {})
        decision_card["user_inputs"] = user_inputs
        state.last_decision_card_json = decision_card
    await session.commit()
    await event_service.append(session, run.id, "user_input.consumed", {
        "input_ids": [item["id"] for item in user_inputs],
        "revisions": [item["revision"] for item in user_inputs],
        "attempt_id": attempt.id if attempt else None,
    })
    return {
        "items": queued,
        "input_ids": [item["id"] for item in user_inputs],
        "user_inputs": user_inputs,
        "text": "\n\n".join(f"User supplemental input v{item['revision']}: {item['content']}" for item in user_inputs),
    }
