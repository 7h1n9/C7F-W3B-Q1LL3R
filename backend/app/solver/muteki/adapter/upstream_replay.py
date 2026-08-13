"""Explicitly gated, read-only replay of an upstream Muteki graph.

This is the first production-integration boundary, not a replacement runtime.
It lets operators validate official graph/event semantics against an existing
run without allowing the upstream package to write the current database or
dispatch tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..upstream_bridge import to_upstream_challenge
from .upstream_events import project_upstream_events


class UpstreamReplayDisabled(RuntimeError):
    """Raised when a caller requests replay without explicitly enabling it."""


@dataclass(frozen=True, slots=True)
class UpstreamReplayConfig:
    """Runtime gate for read-only upstream graph replay."""

    enabled: bool = False

    @classmethod
    def from_environment(cls) -> "UpstreamReplayConfig":
        raw = os.getenv("APP_MUTEKI_UPSTREAM_REPLAY")
        if raw is None:
            raw = os.getenv("MUTEKI_UPSTREAM_REPLAY")
        enabled = bool(raw and raw.strip().casefold() in {"1", "true", "yes", "on"})
        return cls(enabled=enabled)


class UpstreamGraphReplay:
    """Read official graph events and project them into current envelopes."""

    def __init__(self, config: UpstreamReplayConfig | None = None) -> None:
        self.config = config or UpstreamReplayConfig.from_environment()

    def replay_events(
        self,
        *,
        db_path: str | Path,
        challenge: Any,
        after_sequence: int = 0,
    ) -> list[Any]:
        """Replay events from an existing upstream graph without writing it."""

        if not self.config.enabled:
            raise UpstreamReplayDisabled(
                "Set APP_MUTEKI_UPSTREAM_REPLAY=true for explicit read-only replay."
            )

        from muteki.swarm.shared_graph import SQLiteSharedGraph

        mapped = to_upstream_challenge(challenge)
        graph = SQLiteSharedGraph.open_readonly(db_path=db_path, challenge=mapped)
        try:
            events = graph.events_since(max(0, int(after_sequence)))
        finally:
            graph.close()
        return project_upstream_events(events, challenge_id=str(getattr(challenge, "id", "")))


__all__ = [
    "UpstreamGraphReplay",
    "UpstreamReplayConfig",
    "UpstreamReplayDisabled",
]
