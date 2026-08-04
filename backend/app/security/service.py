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
            # The legacy oracle is useful validation evidence, but only an
            # explicit control matrix can establish a successful validation.
            payload = value if isinstance(value, Mapping) else {}
            controls = ValidationControls.model_validate(payload.get("controls") or {})
            status = ValidationStatus.SUCCESS if all(controls.model_dump().values()) else ValidationStatus.INCONCLUSIVE
            confidence = float(self._value(fact, "confidence") or 0) / 100
            return ValidationResult(
                type="SQL_INJECTION_VALIDATION",
                hypothesis_id=str(payload.get("hypothesis_id") or ""),
                status=status,
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
