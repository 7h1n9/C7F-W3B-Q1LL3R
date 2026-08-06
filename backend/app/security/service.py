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
    ValidationEvidence,
    ValidationEvidenceStatus,
    ValidationResult,
    ValidationStatus,
    VulnerabilityHypothesis,
)


class ValidationEvidenceService:
    """Normalize tool-specific validation signals into one result model."""

    def from_result(self, vulnerability_type: str, payload: Mapping[str, Any] | None) -> ValidationEvidence:
        data = dict(payload or {})
        kind = str(vulnerability_type or "UNKNOWN").upper().replace(" ", "_")
        if kind in {"SQL", "SQL_INJECTION", "SQL_INJECTION_VALIDATION"}:
            stable_true = data.get("stable_true") is True
            stable_false = data.get("stable_false") is True
            differential = data.get("boolean_differential", data.get("response_differential", data.get("true_false_differential"))) is True
            confirmed = data.get("boolean_oracle_confirmed") is True
            status = ValidationEvidenceStatus.VALIDATED if stable_true and stable_false and differential and confirmed else ValidationEvidenceStatus.INCONCLUSIVE if not (stable_true and stable_false) else ValidationEvidenceStatus.FAILED
            confidence = 0.95 if status == ValidationEvidenceStatus.VALIDATED else 0.55 if status == ValidationEvidenceStatus.INCONCLUSIVE else 0.35
            return ValidationEvidence(
                vulnerability_type="SQL_INJECTION",
                target=str(data.get("target") or data.get("endpoint") or (data.get("request") or {}).get("url") or ""),
                parameter=str(data.get("parameter") or data.get("test_field") or ""),
                request=data.get("request") or data.get("request_contract") or {},
                response={"true_signature": data.get("true_signature"), "false_signature": data.get("false_signature"), "differential": differential},
                control_group={"stable_true": stable_true, "stable_false": stable_false, "baseline": data.get("baseline")},
                confidence=confidence,
                status=status,
            )

        if kind == "XSS":
            reflected = data.get("payload_reflection", data.get("payload_reflected")) is True
            status = ValidationEvidenceStatus.VALIDATED if reflected else ValidationEvidenceStatus.FAILED if data.get("payload_reflection") is False else ValidationEvidenceStatus.INCONCLUSIVE
            return ValidationEvidence(vulnerability_type="XSS", target=str(data.get("target") or ""), parameter=str(data.get("parameter") or ""), request=data.get("request") or {}, response=data.get("response") or {"reflected": reflected}, control_group=data.get("control_group") or {}, confidence=0.9 if reflected else 0.3, status=status)

        if kind == "SSRF":
            internal = data.get("internal_response_evidence", data.get("internal_response")) is True
            status = ValidationEvidenceStatus.VALIDATED if internal else ValidationEvidenceStatus.FAILED if data.get("internal_response_evidence") is False else ValidationEvidenceStatus.INCONCLUSIVE
            return ValidationEvidence(vulnerability_type="SSRF", target=str(data.get("target") or ""), parameter=str(data.get("parameter") or ""), request=data.get("request") or {}, response=data.get("response") or {"internal": internal}, control_group=data.get("control_group") or {}, confidence=0.9 if internal else 0.3, status=status)

        if kind in {"FILE_UPLOAD", "FILEUPLOAD"}:
            accepted = data.get("upload_accepted") is True and (data.get("retrievable") is True or data.get("server_executed") is True)
            status = ValidationEvidenceStatus.VALIDATED if accepted else ValidationEvidenceStatus.FAILED if data.get("upload_accepted") is False else ValidationEvidenceStatus.INCONCLUSIVE
            return ValidationEvidence(vulnerability_type="FILE_UPLOAD", target=str(data.get("target") or ""), parameter=str(data.get("parameter") or ""), request=data.get("request") or {}, response=data.get("response") or {}, control_group=data.get("control_group") or {}, confidence=0.9 if accepted else 0.3, status=status)

        if kind in {"COMMAND_INJECTION", "COMMANDINJECTION"}:
            executed = data.get("command_executed") is True or data.get("output_differential") is True
            status = ValidationEvidenceStatus.VALIDATED if executed else ValidationEvidenceStatus.FAILED if data.get("command_executed") is False else ValidationEvidenceStatus.INCONCLUSIVE
            return ValidationEvidence(vulnerability_type="COMMAND_INJECTION", target=str(data.get("target") or ""), parameter=str(data.get("parameter") or ""), request=data.get("request") or {}, response=data.get("response") or {}, control_group=data.get("control_group") or {}, confidence=0.9 if executed else 0.3, status=status)

        return ValidationEvidence(vulnerability_type=kind, target=str(data.get("target") or ""), parameter=str(data.get("parameter") or ""), request=data.get("request") or {}, response=data.get("response") or {}, control_group=data.get("control_group") or {}, confidence=0.0, status=ValidationEvidenceStatus.INCONCLUSIVE)


validation_evidence_service = ValidationEvidenceService()


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
        # Read legacy SUCCESS payloads for backward compatibility, but all
        # newly produced and persisted values use the canonical VALIDATED
        # status.
        has_successful_validation = any(str(item.get("status") or "").upper() in {"VALIDATED", "SUCCESS"} for item in validations if isinstance(item, Mapping))
        has_failed_validation = any(str(item.get("status") or "").upper() == "FAILED" for item in validations if isinstance(item, Mapping))

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
                "rule": "ValidationResult VALIDATED proves validation only; continue with ExploitResult and ImpactAssessment.",
            }
        if has_failed_validation:
            return {
                "priority": "SECURITY_CONTEXT",
                "next_action": "CHANGE_VALIDATION_STRATEGY",
                "completion_allowed": False,
                "rule": "A failed validation requires a changed validation strategy.",
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

        if fact_key == "security.sql_injection.validation" or fact_type == "security_validation":
            payload = value if isinstance(value, Mapping) else {}
            raw_confidence = payload.get("confidence")
            if raw_confidence is None:
                raw_confidence = self._value(fact, "confidence")
                raw_confidence = float(raw_confidence or 0) / 100
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                confidence = 0.0
            controls = payload.get("controls")
            if not isinstance(controls, Mapping):
                controls = {
                    "baseline": True,
                    "positive_control": True,
                    "negative_control": True,
                }
            reproduction = payload.get("reproduction")
            if not isinstance(reproduction, Mapping):
                reproduction = {
                    "repeat_count": int(payload.get("repeat_count") or 0),
                    "stable": True,
                }
            return ValidationResult(
                hypothesis_id=str(payload.get("hypothesis_id") or ""),
                type="SQL_INJECTION_VALIDATION",
                status=ValidationStatus.VALIDATED,
                evidence_ids=list(payload.get("evidence_ids") or evidence_ids),
                confidence=min(max(confidence, 0.0), 1.0),
                controls=ValidationControls.model_validate(controls),
                reproduction=reproduction,
            )

        if fact_key == "asset_warranty.mysql_boolean_oracle":
            # Promotion of the durable Boolean Oracle fact is the legacy
            # controller's validation boundary.  It is not an exploit or an
            # impact claim, but it is sufficient to materialize the semantic
            # validation result required by this compatibility layer.
            payload = value if isinstance(value, Mapping) else {}
            embedded = payload.get("validation_result")
            if isinstance(embedded, Mapping):
                return self.validation_result_from_evidence(
                    embedded,
                    hypothesis_id=str(payload.get("hypothesis_id") or ""),
                    evidence_ids=evidence_ids,
                )
            controls = ValidationControls.model_validate(payload.get("controls") or {})
            confidence = float(self._value(fact, "confidence") or 0) / 100
            return ValidationResult(
                type="SQL_INJECTION_VALIDATION",
                hypothesis_id=str(payload.get("hypothesis_id") or ""),
                status=ValidationStatus.VALIDATED,
                evidence_ids=evidence_ids,
                confidence=min(max(confidence, 0.0), 1.0),
                controls=controls,
            )
        return None

    def map_exploit_fact(self, fact: Any) -> ExploitResult | None:
        """Map only durable extraction facts into an ExploitResult.

        Metadata facts are deliberately excluded by fact type. An exploit
        requires a non-empty extracted value produced by bounded extraction.
        """
        fact_key = str(self._value(fact, "fact_key") or "")
        fact_type = str(self._value(fact, "fact_type") or "").upper()
        if fact_key != "security.sql_injection.exploit" and fact_type != "EXPLOIT_RESULT":
            return None

        value = self._value(fact, "value_json")
        payload = value if isinstance(value, Mapping) else {}
        raw_data = payload.get("extracted_data")
        if raw_data is None:
            raw_data = payload.get("extracted_value")
        if isinstance(raw_data, (str, int, float, bool)):
            extracted_data = [raw_data] if str(raw_data).strip() else []
        elif isinstance(raw_data, list):
            extracted_data = [item for item in raw_data if item not in (None, "")]
        else:
            extracted_data = []
        if not extracted_data:
            return None

        raw_confidence = payload.get("confidence")
        if raw_confidence is None:
            raw_confidence = float(self._value(fact, "confidence") or 0) / 100
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        evidence_ids = list(payload.get("evidence_ids") or self._value(fact, "evidence_ids_json") or [])
        return ExploitResult(
            type=str(payload.get("vulnerability_type") or payload.get("type") or "SQL_INJECTION"),
            validation_id=str(payload.get("validation_id") or ""),
            status=ExploitStatus.SUCCESS,
            confidence=min(max(confidence, 0.0), 1.0),
            method=str(payload.get("method") or "BOOLEAN_EXTRACTION"),
            extracted_data=extracted_data,
            evidence_ids=evidence_ids,
            scope=payload.get("scope") or {"type": "DATA_DISCLOSURE", "data_fields": []},
        )

    @staticmethod
    def validation_result_from_evidence(
        evidence: Mapping[str, Any],
        *,
        hypothesis_id: str = "",
        evidence_ids: list[str] | None = None,
    ) -> ValidationResult:
        """Normalize the compatibility evidence shape into ValidationResult.

        The reducer may still attach ValidationEvidence-shaped data to a
        legacy Candidate Fact.  The durable SecurityContext must contain the
        canonical ValidationResult shape so Lifecycle and Finding consume one
        status vocabulary.
        """
        raw_status = str(evidence.get("status") or "INCONCLUSIVE").upper()
        status = (
            ValidationStatus.VALIDATED
            if raw_status in {"VALIDATED", "SUCCESS"}
            else ValidationStatus.FAILED
            if raw_status == "FAILED"
            else ValidationStatus.INCONCLUSIVE
        )
        control_group = evidence.get("control_group") or evidence.get("controls") or {}
        stable_true = bool(control_group.get("stable_true")) if isinstance(control_group, Mapping) else False
        stable_false = bool(control_group.get("stable_false")) if isinstance(control_group, Mapping) else False
        return ValidationResult(
            hypothesis_id=hypothesis_id,
            type="SQL_INJECTION_VALIDATION",
            status=status,
            evidence_ids=list(evidence_ids or evidence.get("evidence_ids") or []),
            confidence=min(max(float(evidence.get("confidence") or 0.0), 0.0), 1.0),
            controls=ValidationControls(
                baseline=True,
                positive_control=stable_true,
                negative_control=stable_false,
            ),
            reproduction={
                "repeat_count": int(evidence.get("repeat_count") or 0),
                "stable": stable_true and stable_false,
            },
        )

    def create_finding(
        self,
        hypothesis: VulnerabilityHypothesis,
        validation: ValidationResult,
        exploit: ExploitResult,
        impact: ImpactAssessment,
    ) -> SecurityFinding | None:
        """Create a conclusion only when the complete evidence chain exists."""
        if validation.status != ValidationStatus.VALIDATED:
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
