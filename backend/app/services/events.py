import asyncio
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
        # This service runs in one backend process. A per-run lock works on SQLite
        # and the row lock/counter remains durable for production database sessions.
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            body = payload or {}
            # Lifecycle events are intentionally separate from internal phase
            # transitions.  This keeps status history useful and prevents a
            # PLANNING/EXECUTING loop from looking like a Run restart.
            if event_type == "run.status_changed" and str(body.get("status", "")) in {
                "PLANNING", "EXECUTING", "EVALUATING"
            }:
                event_type = "run.phase_changed"

            # ``solve_runs.event_sequence`` is a legacy compatibility field;
            # it is no longer updated for every event.  Ordering is owned by
            # RunEvent.event_id, while sequence remains a per-run API cursor.
            sequence = int(
                await session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)) or 0
            ) + 1
            # MySQL assigns the global BIGINT AUTO_INCREMENT event_id.  The
            # SQLite/dev schema keeps the field nullable and uses a durable
            # per-run fallback so old dumps remain insertable.
            event_id = None
            if not (session.bind and session.bind.dialect.name in {"mysql", "mariadb"}):
                event_id = int(
                    await session.scalar(select(func.max(RunEvent.event_id)).where(RunEvent.run_id == run_id)) or 0
                ) + 1
            event = RunEvent(
                run_id=run_id, event_id=event_id, sequence=sequence, event_type=event_type, payload_json=body
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
                    # MySQL does not support PostgreSQL's ``NULLS LAST``
                    # syntax.  Old SQLite rows can have a null event_id, so
                    # use the per-run sequence as a portable fallback.
                    .order_by(func.coalesce(RunEvent.event_id, RunEvent.sequence), RunEvent.sequence)
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
