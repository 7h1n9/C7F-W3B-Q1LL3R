"""Security-semantic phase decisions for the controller."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SecurityDecision:
    required_phase: str
    reporting_allowed: bool
    reason: str
    confidence: float = 1.0


class SecurityDecisionEngine:
    """Derive the next security phase without changing legacy stage logic."""

    def decide(self, security_context: Mapping[str, Any] | None) -> SecurityDecision | None:
        context = security_context or {}
        findings = self._items(context, "findings")
        impacts = self._items(context, "impact_assessments")
        exploits = self._items(context, "exploit_results")
        validations = self._items(context, "validation_results")
        hypotheses = self._items(context, "hypotheses")
        information = self._items(context, "information_evidence")

        if self._has_status(findings, "CREATED"):
            return SecurityDecision(
                required_phase="REPORTING",
                reporting_allowed=True,
                reason="A SecurityFinding with CREATED status is available.",
            )
        if impacts:
            return SecurityDecision(
                required_phase="REPORTING",
                reporting_allowed=False,
                reason="ImpactAssessment is ready for reporting, but SecurityFinding is not created.",
            )
        if self._has_status(exploits, "SUCCESS"):
            return SecurityDecision(
                required_phase="IMPACT",
                reporting_allowed=False,
                reason="ExploitResult succeeded; impact must be assessed.",
            )
        if self._has_status(validations, "SUCCESS") or self._has_status(validations, "VALIDATED"):
            return SecurityDecision(
                required_phase="EXPLOITATION",
                reporting_allowed=False,
                reason="ValidationResult succeeded; exploitation must confirm security impact.",
            )
        if self._has_status(validations, "FAILED"):
            return SecurityDecision(
                required_phase="VALIDATION",
                reporting_allowed=False,
                reason="Validation failed; a different validation strategy is required.",
            )
        if self._has_status(validations, "INCONCLUSIVE"):
            return SecurityDecision(
                required_phase="VALIDATION",
                reporting_allowed=False,
                reason="Validation is inconclusive; continue controlled validation.",
            )
        if hypotheses:
            return SecurityDecision(
                required_phase="VALIDATION",
                reporting_allowed=False,
                reason="A vulnerability hypothesis requires validation.",
            )
        if information:
            return SecurityDecision(
                required_phase="HYPOTHESIS",
                reporting_allowed=False,
                reason="InformationEvidence is environmental information, not vulnerability completion.",
            )
        return None

    def evaluate(self, security_context: Mapping[str, Any] | None) -> SecurityDecision | None:
        """Compatibility name for controller-facing decision evaluation."""
        return self.decide(security_context)

    @staticmethod
    def _items(context: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        return [item for item in (context.get(key) or []) if isinstance(item, Mapping)]

    @staticmethod
    def _has_status(items: list[Mapping[str, Any]], status: str) -> bool:
        return any(str(item.get("status") or "").upper() == status for item in items)


security_decision_engine = SecurityDecisionEngine()
