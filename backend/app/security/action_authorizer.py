"""Action authorization boundary for the state-driven Solver.

This module deliberately does not execute tools, mutate Blackboard, or make
strategy decisions.  It only returns a structured authorization decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.solver.action import ActionIntent
    from app.solver.context import RunContext


class SecurityDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ActionSecurityDecision:
    decision: SecurityDecisionType
    reason: str | None = None
    policy_id: str | None = None
    reason_code: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is SecurityDecisionType.ALLOW


class ActionAuthorizer(Protocol):
    def authorize(
        self,
        action: "ActionIntent",
        context: "RunContext | None",
    ) -> ActionSecurityDecision: ...


class AllowAllActionAuthorizer:
    """Compatibility policy preserving the Phase 2.1 execution behavior."""

    def __init__(self, *, policy_id: str = "solver-allow-all") -> None:
        self.policy_id = policy_id

    def authorize(
        self,
        action: "ActionIntent",
        context: "RunContext | None",
    ) -> ActionSecurityDecision:
        return ActionSecurityDecision(
            decision=SecurityDecisionType.ALLOW,
            reason="compatibility authorizer allows the action",
            policy_id=self.policy_id,
            reason_code="ALLOW",
        )
