"""Compatibility mapping and safety gates for web security conclusions."""

from collections.abc import Mapping
from typing import Any

from app.security.schemas import (
    ExploitResult,
    ExploitStatus,
    FindingStatus,
    ImpactAssessment,
    InformationEvidence,
    SecurityFinding,
    ValidationControls,
    ValidationResult,
    ValidationStatus,
    VulnerabilityHypothesis,
)


INFORMATION_FACT_MARKERS = (
    "mysql_version",
    "database()",
    "version()",
    "information_schema",
    "mysql_metadata",
)


class SecurityFindingService:
    """Translate legacy facts without promoting information into findings."""

    @staticmethod
    def planner_guidance(context: Mapping[str, Any] | None) -> dict[str, Any]:
        """Return deterministic Planner guidance from the current security blackboard."""
        security = context or {}
        findings = list(security.get("findings") or [])
        validations = list(security.get("validation_results") or [])
        hypotheses = list(security.get("hypotheses") or [])
        information = list(security.get("information_evidence") or [])
        has_created_finding = any(str(item.get("status") or "").upper() == "CREATED" for item in findings if isinstance(item, Mapping))
        has_successful_validation = any(str(item.get("status") or "").upper() == "SUCCESS" for item in validations if isinstance(item, Mapping))

        if has_created_finding:
            return {
                "priority": "SECURITY_CONTEXT",
                "next_action": "REPORTING",
                "completion_allowed": True,
                "rule": "SecurityFinding CREATED is required before reporting.",
            }
        if has_successful_validation:
            return {
                "priority": "SECURITY_CONTEXT",
                "next_action": "EXPLOIT_IMPACT_CONFIRMATION",
                "completion_allowed": False,
                "rule": "ValidationResult SUCCESS proves validation only; continue with ExploitResult and ImpactAssessment.",
            }
        if hypotheses or validations or information:
            return {
                "priority": "SECURITY_CONTEXT",
                "next_action": "HYPOTHESIS_VALIDATION",
                "completion_allowed": False,
                "rule": "InformationEvidence and open/inconclusive validation state do not prove a vulnerability.",
            }
        return {
            "priority": "SECURITY_CONTEXT",
            "next_action": "DISCOVER_OR_FORM_HYPOTHESIS",
            "completion_allowed": False,
            "rule": "Security reasoning must be established before reporting.",
        }

    def map_verified_fact(self, fact: Any) -> InformationEvidence | ValidationResult | None:
        fact_key = str(self._value(fact, "fact_key") or "")
        fact_type = str(self._value(fact, "fact_type") or "").lower()
        evidence_ids = list(self._value(fact, "evidence_ids_json") or [])
        fact_id = str(self._value(fact, "id") or "")
        value = self._value(fact, "value_json")

        if self._is_information_fact(fact_key, fact_type):
            return InformationEvidence(
                fact_id=fact_id,
                fact_key=fact_key,
                value=value,
                evidence_ids=evidence_ids,
            )

        if fact_key == "asset_warranty.mysql_boolean_oracle":
            # Promotion of the durable Boolean Oracle fact is the legacy
            # controller's validation boundary.  It is not an exploit or an
            # impact claim, but it is sufficient to materialize the semantic
            # validation result required by this compatibility layer.
            payload = value if isinstance(value, Mapping) else {}
            controls = ValidationControls.model_validate(payload.get("controls") or {})
            confidence = float(self._value(fact, "confidence") or 0) / 100
            return ValidationResult(
                type="SQL_INJECTION_VALIDATION",
                hypothesis_id=str(payload.get("hypothesis_id") or ""),
                status=ValidationStatus.SUCCESS,
                evidence_ids=evidence_ids,
                confidence=min(max(confidence, 0.0), 1.0),
                controls=controls,
            )
        return None

    def create_finding(
        self,
        hypothesis: VulnerabilityHypothesis,
        validation: ValidationResult,
        exploit: ExploitResult,
        impact: ImpactAssessment,
    ) -> SecurityFinding | None:
        """Create a conclusion only when the complete evidence chain exists."""
        if validation.status != ValidationStatus.SUCCESS:
            return None
        if validation.hypothesis_id != hypothesis.id or exploit.validation_id != validation.id:
            return None
        if exploit.status != ExploitStatus.SUCCESS or not exploit.evidence_ids:
            return None
        if impact.exploit_id != exploit.id or not impact.evidence_ids or not impact.business_impact.strip():
            return None
        confidence = min(hypothesis.confidence, validation.confidence, 1.0)
        return SecurityFinding(
            vulnerability_type=hypothesis.type,
            status=FindingStatus.CREATED,
            hypothesis_id=hypothesis.id,
            validation_id=validation.id,
            exploit_id=exploit.id,
            impact_id=impact.id,
            confidence=confidence,
        )

    @staticmethod
    def _value(obj: Any, key: str) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(key)
        return getattr(obj, key, None)

    @staticmethod
    def _is_information_fact(fact_key: str, fact_type: str) -> bool:
        normalized = fact_key.lower()
        return any(marker in normalized for marker in INFORMATION_FACT_MARKERS) or "metadata" in fact_type


security_finding_service = SecurityFindingService()
