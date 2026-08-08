from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .action import ActionIntent


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str
    phase: str
    action_name: str

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


class ActionPolicyValidator:
    """Validate an ActionIntent against the independent solver phase policy."""

    ACTIONS: dict[str, frozenset[str]] = {
        "BASELINE": frozenset({"http_request"}),
        "VALIDATION": frozenset({"sql_boolean_compare"}),
        "EXPLOITATION": frozenset({
            "data_extraction",
            "oracle_expression_calibration",
            "mysql_metadata_discovery",
            "sql_extract",
            "request_capture",
            "sqlmap_detect",
            "sqlmap_run",
            "sqlite_metadata_discovery",
            "script_run",
        }),
        "IMPACT": frozenset({"impact_validation"}),
        "REPORTING": frozenset({"report"}),
    }

    def validate(self, phase: str, intent: ActionIntent) -> PolicyResult:
        normalized_phase = str(phase or "").upper()
        allowed = self.ACTIONS.get(normalized_phase, frozenset())
        if intent.action_name in allowed:
            return PolicyResult(
                decision=PolicyDecision.ALLOW,
                reason="action is allowed for the current solver phase",
                phase=normalized_phase,
                action_name=intent.action_name,
            )
        return PolicyResult(
            decision=PolicyDecision.DENY,
            reason=f"{intent.action_name!r} is not allowed during {normalized_phase or 'UNKNOWN'}",
            phase=normalized_phase,
            action_name=intent.action_name,
        )

    def check(self, phase: str, intent: ActionIntent) -> PolicyResult:
        """Readable alias for callers that treat policy as a check."""
        return self.validate(phase, intent)
