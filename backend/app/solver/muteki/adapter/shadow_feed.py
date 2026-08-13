"""Optional upstream-to-SSE shadow event feed."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .event_bridge import EventBridge
from .upstream_replay import UpstreamGraphReplay


class UpstreamShadowFeed:
    """Publish upstream replay events without mixing them with live events."""

    def __init__(self, replay: UpstreamGraphReplay | None = None) -> None:
        self._replay = replay or UpstreamGraphReplay()

    async def publish(
        self,
        *,
        db_path: str | Path,
        challenge: Any,
        bridge: EventBridge,
        after_sequence: int = 0,
    ) -> int:
        """Project and publish events under the ``shadow`` event namespace."""

        events = self._replay.replay_events(
            db_path=db_path,
            challenge=challenge,
            after_sequence=after_sequence,
        )
        for event in events:
            await bridge.bridge(replace(event, event_type=f"shadow.{event.event_type}"))
        return len(events)


__all__ = ["UpstreamShadowFeed"]
