from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .graph import MutekiGraph


@dataclass(frozen=True, slots=True)
class IntentProposal:
    goal: str
    worker_class: str = "code"
    rationale: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReasonResult:
    goal_met: bool
    intents: tuple[IntentProposal, ...]
    verdict: str = "explore"
    drift: str = ""


ReasonProvider = Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any] | str] | Awaitable[Sequence[Mapping[str, Any] | str]]]


class MutekiReason:
    """Cheap, graph-only planner that produces bounded claimable intents."""

    def __init__(self, provider: ReasonProvider | None = None, *, max_intents: int = 4) -> None:
        self.provider = provider
        self.max_intents = max(1, min(max_intents, 4))

    async def reason(self, graph: MutekiGraph) -> ReasonResult:
        snapshot = graph.snapshot()
        if snapshot["flags"]:
            return ReasonResult(True, (), verdict="complete")
        open_goals = {
            item["description"].casefold()
            for item in snapshot["intents"]
            if item["status"] in {"open", "claimed"}
        }
        dead_ends = {item["description"].casefold() for item in snapshot["dead_ends"]}
        if self.provider is None:
            raw: Sequence[Mapping[str, Any] | str] = ("discover an untested target surface",)
        else:
            raw_value = self.provider(snapshot)
            raw = await raw_value if inspect.isawaitable(raw_value) else raw_value
        proposals: list[IntentProposal] = []
        for item in raw:
            if isinstance(item, Mapping):
                goal = item.get("goal") or item.get("description")
                worker_class = item.get("worker_class", "code")
                rationale = item.get("rationale", "")
                payload = item.get("payload", {})
            else:
                goal, worker_class, rationale, payload = item, "code", "", {}
            if not isinstance(goal, str) or not goal.strip():
                continue
            normalized = goal.strip().casefold()
            if normalized in open_goals or any(dead and dead in normalized for dead in dead_ends):
                continue
            proposals.append(IntentProposal(goal.strip(), str(worker_class), str(rationale), dict(payload) if isinstance(payload, Mapping) else {}))
            if len(proposals) >= self.max_intents:
                break
        return ReasonResult(False, tuple(proposals))

    def write_intents(self, graph: MutekiGraph, result: ReasonResult, *, actor: str = "coordinator") -> list[str]:
        return [
            graph.propose_intent(actor=actor, description=item.goal, payload={"worker_class": item.worker_class, "rationale": item.rationale, **item.payload})
            for item in result.intents
        ]


__all__ = ["IntentProposal", "MutekiReason", "ReasonResult"]
