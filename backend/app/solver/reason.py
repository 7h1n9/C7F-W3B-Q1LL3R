from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReasonIntent:
    """A natural-language next step proposed by the coordinator brain."""

    description: str


PlannerProvider = Callable[[Mapping[str, Any]], Sequence[str | Mapping[str, Any]] | Awaitable[Sequence[str | Mapping[str, Any]]]]


class ReasonPlanner:
    """Read-only planner for SharedGraph coordination.

    The provider is injected so the coordinator can use codex-bridge or a
    lightweight model.  The deterministic fallback is deliberately modest: it
    produces useful bounded intents without becoming an exploit engine.
    """

    def __init__(self, provider: PlannerProvider | None = None, *, max_intents: int = 4) -> None:
        self.provider = provider
        self.max_intents = max(1, min(max_intents, 4))

    async def plan(self, snapshot: Mapping[str, Any]) -> list[ReasonIntent]:
        if self.provider is not None:
            proposed = self.provider(snapshot)
            if inspect.isawaitable(proposed):
                proposed = await proposed
            intents = self._normalize(proposed)
        else:
            intents = self._fallback(snapshot)
        deadends = {
            str(item.get("description", "")).casefold()
            for item in snapshot.get("deadends", [])
            if isinstance(item, Mapping)
        }
        return [item for item in intents if item.description.casefold() not in deadends][: self.max_intents]

    def _normalize(self, values: Sequence[str | Mapping[str, Any]]) -> list[ReasonIntent]:
        result: list[ReasonIntent] = []
        for value in values:
            if isinstance(value, Mapping):
                description = value.get("description") or value.get("intent")
            else:
                description = value
            if isinstance(description, str) and description.strip():
                result.append(ReasonIntent(description.strip()))
        return result

    def _fallback(self, snapshot: Mapping[str, Any]) -> list[ReasonIntent]:
        facts = snapshot.get("facts", [])
        if not facts:
            return [ReasonIntent("discover the target surface and record only observed endpoints")]
        if not snapshot.get("flags"):
            return [ReasonIntent("review verified facts and test the highest-confidence unexplored path")]
        return []
