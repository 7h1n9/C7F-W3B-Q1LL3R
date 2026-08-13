from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.services.events import EventService

from ..events import EventEnvelope


_MUTEKI_PHASE_TO_RUN_PHASE = {
    "prepare": "PREPARE",
    "race": "RACE",
    "coordinator": "COORDINATOR",
    "finalize": "FINALIZE",
}


class EventBridge:
    """Bridge canonical graph events into the existing durable SSE stream."""

    def __init__(self, session: Any, *, service: EventService | None = None, run_id: str | None = None) -> None:
        self._session = session
        # Muteki graph callbacks are ordered by this bridge. Keep their
        # asyncio lock separate from ToolGateway's EventService lock: a
        # worker can emit a tool event while the preceding Muteki audit event
        # is still being flushed. Both services persist to the same durable
        # RunEvent table, and EventService already retries sequence races.
        self._service = service or EventService()
        self._run_id = run_id
        self._seen: set[tuple[str, str, int]] = set()
        self._tail: asyncio.Task[Any] | None = None
        self._buffered: list[EventEnvelope] = []

    async def bridge(self, event: EventEnvelope) -> Any:
        run_id = str(self._run_id or event.challenge_id)
        # Include the event namespace: a read-only upstream shadow feed may
        # legitimately reuse the same sequence numbers as the current graph.
        key = (run_id, str(event.event_type), int(event.sequence))
        if key in self._seen:
            return None
        self._seen.add(key)
        payload = {
            "muteki_sequence": event.sequence,
            "muteki_event_type": str(event.event_type),
            "actor": event.actor,
            "verified": event.verified,
            "confidence": event.confidence,
            "payload": self._safe_payload(event.payload),
        }
        if hasattr(self._session, "__call__"):
            async with self._session() as session:
                result = await self._service.append(session, run_id, f"muteki.{event.event_type}", payload)
                await self._project_run_phase(session, run_id, event)
                return result
        result = await self._service.append(self._session, run_id, f"muteki.{event.event_type}", payload)
        await self._project_run_phase(self._session, run_id, event)
        return result

    @staticmethod
    async def _project_run_phase(session: Any, run_id: str, event: EventEnvelope) -> None:
        """Project canonical Muteki stage changes onto the outer Run view.

        The official graph remains the phase authority.  This is only a
        lifecycle/view projection: it does not change RunStatus, perform a
        transition, or write any solver state.  The outer supervisor still
        owns terminal transitions and overwrites the final view with
        ``REPORTING`` after the runtime returns.
        """

        if str(event.event_type) != "phase_changed":
            return
        event_payload = event.payload if isinstance(event.payload, dict) else {}
        phase = str(event_payload.get("phase") or "").strip().casefold()
        run_phase = _MUTEKI_PHASE_TO_RUN_PHASE.get(phase)
        if not run_phase:
            return
        try:
            from sqlalchemy import select

            from app.models.run import SolveRun

            run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
            if run is None:
                return
            # A terminal outer lifecycle owns its reporting view.  Late event
            # delivery must not regress a completed/failed Run back to a
            # canonical Muteki stage.
            if str(run.status) in {
                "COMPLETED_SOLVED",
                "COMPLETED_UNSOLVED",
                "FAILED_ENGINE",
                "FAILED_TOOL",
                "FAILED_RUNNER",
                "TIMEOUT",
                "CANCELLED",
            }:
                return
            run.current_phase = run_phase
            await session.commit()
        except Exception:
            # Event projection is observability-only.  A stale test session,
            # shutdown race, or unavailable lifecycle row must not interrupt
            # the canonical Graph/Worker loop.
            try:
                await session.rollback()
            except Exception:
                pass

    def callback(self) -> Callable[[EventEnvelope], None]:
        def submit(event: EventEnvelope) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._buffered.append(event)
                return
            try:
                previous = self._tail

                async def ordered() -> Any:
                    if previous is not None:
                        await previous
                    return await self.bridge(event)

                task = loop.create_task(ordered())
            except RuntimeError:
                self._buffered.append(event)
                return
            self._tail = task

        return submit

    async def flush(self) -> None:
        buffered = tuple(self._buffered)
        self._buffered.clear()
        for event in buffered:
            await self.bridge(event)
        if self._tail is not None:
            await asyncio.gather(self._tail, return_exceptions=True)

    @staticmethod
    def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
        blocked = {"raw", "raw_result", "response", "body", "cookie", "token", "secret", "password", "ground_truth"}
        return {str(key): value for key, value in payload.items() if str(key).casefold() not in blocked}


__all__ = ["EventBridge"]
