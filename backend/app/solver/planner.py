from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from .action import ActionIntent
from .blackboard.models import BlackboardState

AllowedAction = str | Mapping[str, object]


class Planner(Protocol):
    """Select one intent from StateMachine-provided actions only."""

    def choose(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None: ...


class DeterministicPlanner:
    """Small local Planner Adapter; no model, tool, or runtime integration."""

    def choose(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None:
        if not allowed_actions:
            return None

        descriptor = allowed_actions[0]
        if isinstance(descriptor, Mapping):
            action_name = str(descriptor.get("name") or "")
            purpose = str(descriptor.get("purpose") or action_name)
            suggested_parameters = descriptor.get("parameters")
            parameters = dict(suggested_parameters) if isinstance(suggested_parameters, Mapping) else {}
        else:
            action_name = str(descriptor)
            purpose = action_name
            parameters = {}

        if not action_name:
            return None

        # The adapter may fill a generic target from Blackboard knowledge, but
        # it never invents an action outside the supplied allowed list.
        if action_name == "http_request" and "url" not in parameters:
            target_url = state.knowledge.get("target_url")
            if target_url:
                parameters["method"] = "GET"
                parameters["url"] = str(target_url)

        return ActionIntent(
            action_name=action_name,
            reason=f"select allowed action: {purpose}",
            parameters=parameters,
            metadata={"phase": state.phase, "source": "deterministic_planner"},
        )


class NoopPlanner:
    """Placeholder planner retained for the Coordinator skeleton."""

    def choose(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None:
        return None


class SolverIntent(ActionIntent):
    """Phase 1.1 constructor compatibility; new code uses ActionIntent."""

    def __init__(self, action: str, arguments: dict | None = None) -> None:
        object.__setattr__(self, "action_name", action)
        object.__setattr__(self, "reason", "legacy solver intent")
        object.__setattr__(self, "parameters", dict(arguments or {}))
        object.__setattr__(self, "metadata", {"source": "phase_1_1_compatibility"})

    @property
    def arguments(self) -> dict:
        return dict(self.parameters)
