"""Security-driven vulnerability lifecycle evaluation.

This module is intentionally a pure decision layer.  It does not mutate the
blackboard or create security objects; the Controller remains responsible for
persisting state and dispatching the next bounded phase.
"""

from collections.abc import Mapping
from typing import Any


class VulnerabilityLifecycleEngine:
    """Derive the next security lifecycle state from a SecurityContext."""

    def evaluate(self, security_context: Mapping[str, Any] | None) -> dict[str, Any]:
        context = security_context or {}
        findings = self._items(context, "findings")
        impacts = self._items(context, "impact_assessments")
        exploits = self._items(context, "exploit_results")
        validations = self._items(context, "validation_results")
        hypotheses = self._items(context, "hypotheses")
        information = self._items(context, "information_evidence")

        finding = self._first_with_status(findings, "CREATED")
        if finding is not None:
            return self._result(
                "COMPLETED",
                "REPORTING",
                "SecurityFinding CREATED is the only security conclusion allowed to complete the lifecycle.",
                finding,
                1.0,
            )

        impact = self._first_with_status(impacts, "CONFIRMED")
        if impact is not None:
            return self._result(
                "IMPACT_CONFIRMED",
                "REPORTING",
                "ImpactAssessment is confirmed; create or consume the SecurityFinding before reporting.",
                impact,
                0.95,
            )

        exploit = self._first_with_status(exploits, "SUCCESS")
        if exploit is not None:
            return self._result(
                "EXPLOITED",
                "IMPACT",
                "ExploitResult succeeded; the business and technical impact must be confirmed.",
                exploit,
                0.9,
            )

        validation = self._first_with_status(validations, "VALIDATED", "SUCCESS")
        if validation is not None:
            return self._result(
                "VALIDATED",
                "EXPLOITATION",
                "ValidationResult is successful; exploitation is required to establish real impact.",
                validation,
                0.9,
            )

        if hypotheses:
            return self._result(
                "HYPOTHESIS",
                "VALIDATION",
                "A vulnerability hypothesis exists without a successful validation result.",
                hypotheses[0],
                0.6,
            )

        if information:
            return self._result(
                "HYPOTHESIS",
                "HYPOTHESIS",
                "InformationEvidence is environmental information, not vulnerability confirmation.",
                information[0],
                0.5,
            )

        return {
            "current_state": "INITIAL",
            "required_phase": "HYPOTHESIS",
            "reason": "No security hypothesis or vulnerability evidence exists yet.",
            "confidence": 0.0,
        }

    @staticmethod
    def _items(context: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        return [item for item in (context.get(key) or []) if isinstance(item, Mapping)]

    @classmethod
    def _first_with_status(
        cls,
        items: list[Mapping[str, Any]],
        *statuses: str,
    ) -> Mapping[str, Any] | None:
        wanted = {status.upper() for status in statuses}
        return next(
            (item for item in items if str(item.get("status") or "").upper() in wanted),
            None,
        )

    @staticmethod
    def _result(
        current_state: str,
        required_phase: str,
        reason: str,
        source: Mapping[str, Any],
        default_confidence: float,
    ) -> dict[str, Any]:
        raw_confidence = source.get("confidence")
        try:
            confidence = float(raw_confidence) if raw_confidence is not None else default_confidence
        except (TypeError, ValueError):
            confidence = default_confidence
        return {
            "current_state": current_state,
            "required_phase": required_phase,
            "reason": reason,
            "confidence": min(max(confidence, 0.0), 1.0),
        }


vulnerability_lifecycle_engine = VulnerabilityLifecycleEngine()
