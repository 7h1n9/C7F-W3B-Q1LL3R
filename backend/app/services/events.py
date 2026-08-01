import asyncio
import hashlib
import json
import random

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import RunEvent
from app.orchestration.event_bus import event_bus


class EventService:
    def __init__(self) -> None:
        self._run_locks: dict[str, asyncio.Lock] = {}

    async def append(
        self, session: AsyncSession, run_id: str, event_type: str, payload: dict | None = None
    ) -> RunEvent:
        # This service runs in one backend process. A per-run lock complements
        # the durable MySQL row lock/counter.
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            body = payload or {}
            encoded_body = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode()
            # Status and solver phase are different state machines.  Never
            # infer a phase event from a lifecycle status event.
            if event_type == "run.phase_changed":
                previous = str(body.get("previous_phase") or "")
                current = str(body.get("phase") or body.get("current_phase") or "")
                if not previous or previous == current:
                    event_type = "run.status_changed"
                    body = {**body, "phase_event_suppressed": True}

            # ``solve_runs.event_sequence`` is a legacy compatibility field;
            # it is no longer updated for every event.  Ordering is owned by
            # RunEvent.event_id, while sequence remains a per-run API cursor.
            sequence = int(
                await session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)) or 0
            ) + 1
            event = RunEvent(
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                payload_json=body,
                payload_size=len(encoded_body),
                payload_digest=hashlib.sha256(encoded_body).hexdigest(),
            )
            session.add(event)
            await session.flush()
            for retry in range(3):
                try:
                    await session.commit()
                    break
                except OperationalError as error:
                    code = error.orig.args[0] if getattr(error, "orig", None) and error.orig.args else None
                    if code not in {1205, 1213} or retry == 2:
                        raise
                    await session.rollback()
                    await asyncio.sleep(0.03 * (2**retry) + random.random() * 0.02)
            await session.refresh(event)
        await event_bus.publish(run_id, self.serialize(event))
        return event

    async def history(self, session: AsyncSession, run_id: str, after: int = 0) -> list[RunEvent]:
        return list(
            (
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
                    .order_by(RunEvent.event_id, RunEvent.sequence)
                )
            ).all()
        )

    @staticmethod
    def serialize(event: RunEvent) -> dict:
        return {
            "id": event.id,
            "run_id": event.run_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "payload_json": event.payload_json,
            "created_at": event.created_at.isoformat(),
        }


event_service = EventService()
