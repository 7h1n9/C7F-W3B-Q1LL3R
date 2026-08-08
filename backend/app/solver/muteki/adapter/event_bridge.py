from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.services.events import EventService, event_service

from ..events import EventEnvelope


class EventBridge:
    """Bridge canonical graph events into the existing durable SSE stream."""

    def __init__(self, session: Any, *, service: EventService | None = None, run_id: str | None = None) -> None:
        self._session = session
        self._service = service or event_service
        self._run_id = run_id
        self._seen: set[tuple[str, int]] = set()
        self._tail: asyncio.Task[Any] | None = None
        self._buffered: list[EventEnvelope] = []

    async def bridge(self, event: EventEnvelope) -> Any:
        run_id = str(self._run_id or event.challenge_id)
        key = (run_id, int(event.sequence))
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
                return await self._service.append(session, run_id, f"muteki.{event.event_type}", payload)
        return await self._service.append(self._session, run_id, f"muteki.{event.event_type}", payload)

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
