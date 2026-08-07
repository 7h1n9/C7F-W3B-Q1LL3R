from __future__ import annotations

from typing import Protocol

from .blackboard.models import BlackboardState


class SolverIntent:
    """A single bounded action selected for one Coordinator tick."""

    def __init__(self, action: str, arguments: dict | None = None) -> None:
        self.action = action
        self.arguments = dict(arguments or {})


class Planner(Protocol):
    def choose(self, state: BlackboardState, allowed_actions: list[str]) -> SolverIntent | None: ...


class NoopPlanner:
    """Placeholder planner; it deliberately schedules no action."""

    def choose(self, state: BlackboardState, allowed_actions: list[str]) -> SolverIntent | None:
        return None
