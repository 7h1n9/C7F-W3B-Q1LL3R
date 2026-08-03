"""Guard against terminal WP immediately after consuming user input."""

from sqlalchemy import select

from app.models.run import RunEvent


RESUME_EVENTS = {
    "planner.task.created", "agent.task.created", "analysis.review.created",
    "analysis.plan_review.dispatched", "approved_action.created", "tool.requested", "tool.started",
}


async def check_user_input_resume(session, run_id: str) -> dict:
    events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence))).all())
    consumed = [event for event in events if event.event_type in {"user_input.consumed", "user.input_consumed"}]
    if not consumed:
        return {"ok": True, "last_input_id": None, "last_input_sequence": None}
    latest = consumed[-1]
    resumed = next((event for event in events if event.sequence > latest.sequence and event.event_type in RESUME_EVENTS), None)
    return {
        "ok": resumed is not None,
        "last_input_id": (latest.payload_json or {}).get("input_id") or (latest.payload_json or {}).get("id"),
        "last_input_sequence": latest.sequence,
        "resume_event": resumed.event_type if resumed else None,
        "expected": ["planner", "analysis", "approved_action", "tool"],
    }
