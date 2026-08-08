from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Subscriber = Callable[["EventEnvelope"], None]
EVENT_TYPES = frozenset(
    {
        "WORKER_STARTED",
        "WORKER_FINISHED",
        "FACT_WRITTEN",
        "POC_WRITTEN",
        "DEADEND_MARKED",
        "INTENT_PROPOSED",
        "INTENT_CLAIMED",
        "INTENT_DONE",
        "FLAG_FOUND",
        "FLAG_CANDIDATE",
        "PHASE_CHANGED",
        "RUN_FINISHED",
    }
)
_SENSITIVE_KEYS = frozenset({"raw", "response", "output", "cookie", "token", "secret", "ground_truth"})


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """An ordered, replayable event without unbounded execution output."""

    sequence: int
    event_type: str
    run_id: str
    payload: dict[str, Any]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.sequence,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }


class InsightBus:
    """Small in-process pub/sub bus used by SharedGraph writers."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.RLock()

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, event: EventEnvelope) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            callback(event)


class SolverEventBus(InsightBus):
    """InsightBus with monotonic sequence numbers and optional JSONL storage."""

    def __init__(self, log_path: str | Path | None = None) -> None:
        super().__init__()
        self._log_path = Path(log_path) if log_path else None
        self._sequence = self._read_last_sequence()
        self._write_lock = threading.RLock()
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_last_sequence(self) -> int:
        if self._log_path is None or not self._log_path.exists():
            return 0
        last = 0
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            try:
                last = max(last, int(json.loads(line).get("id", 0)))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return last

    def emit(
        self,
        event_type: str,
        *,
        run_id: str,
        payload: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unsupported coordination event type: {event_type}")
        self._assert_safe_payload(payload or {})
        with self._write_lock:
            self._sequence += 1
            event = EventEnvelope(
                sequence=self._sequence,
                event_type=event_type,
                run_id=run_id,
                payload=dict(payload or {}),
                timestamp=datetime.now(UTC).isoformat(),
            )
            if self._log_path:
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        self.publish(event)
        return event

    @classmethod
    def _assert_safe_payload(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).casefold()
                if any(marker in lowered for marker in _SENSITIVE_KEYS):
                    raise ValueError(f"Sensitive event field is not allowed: {key}")
                cls._assert_safe_payload(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._assert_safe_payload(item)

    def replay(
        self,
        *,
        run_id: str | None = None,
        last_event_id: int = 0,
    ) -> Iterable[EventEnvelope]:
        if self._log_path is None or not self._log_path.exists():
            return ()
        events: list[EventEnvelope] = []
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if int(item["id"]) <= last_event_id:
                    continue
                if run_id is not None and item["run_id"] != run_id:
                    continue
                events.append(
                    EventEnvelope(
                        sequence=int(item["id"]),
                        event_type=str(item["event_type"]),
                        run_id=str(item["run_id"]),
                        payload=dict(item.get("payload", {})),
                        timestamp=str(item["timestamp"]),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(events)
