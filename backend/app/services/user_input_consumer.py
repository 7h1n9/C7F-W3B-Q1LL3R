"""Durable user-input consumption shared by API, Supervisor and Agent runs."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.run import RunAttempt, RunUserInput, SolveRun
from app.models.solver_state import SolverState
from app.services.events import event_service


async def consume_user_inputs(session, run: SolveRun, attempt: RunAttempt | None = None, *, wake_supervisor: bool = True) -> dict:
    queued = list((await session.scalars(select(RunUserInput).where(
        RunUserInput.run_id == run.id,
        RunUserInput.status == "QUEUED",
        RunUserInput.consumed_at.is_(None),
    ).order_by(RunUserInput.revision, RunUserInput.created_at).with_for_update())).all())
    if not queued:
        return {"items": [], "input_ids": [], "user_inputs": [], "text": ""}

    # API/user-input recovery can arrive after the previous attempt has been
    # closed.  Create the bounded execution context before recording the
    # consumed event so the event is resumable and carries an attempt_id.
    if attempt is None and wake_supervisor and str(run.status) in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
        from app.services.run_attempts import run_attempt_service

        attempt, _ = await run_attempt_service.begin(session, run)

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
    checkpoint["user_input_resume_pending"] = True
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
        "run_id": run.id,
        "input_ids": [item["id"] for item in user_inputs],
        "revision": user_inputs[0]["revision"] if len(user_inputs) == 1 else [item["revision"] for item in user_inputs],
        "revisions": [item["revision"] for item in user_inputs],
        "attempt_id": attempt.id if attempt else None,
    })
    if wake_supervisor:
        from app.services.run_supervisor import run_supervisor

        await run_supervisor.enqueue(run.id, reason="USER_INPUT_CONSUMED")
    return {
        "items": queued,
        "input_ids": [item["id"] for item in user_inputs],
        "user_inputs": user_inputs,
        "text": "\n\n".join(f"User supplemental input v{item['revision']}: {item['content']}" for item in user_inputs),
    }
