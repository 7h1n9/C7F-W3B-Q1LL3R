"""Typed security semantics layered above Evidence and VerifiedFact.

These are deliberately application objects, not ORM rows.  The durable
blackboard stores their JSON representation in SolverState so the existing
fact/review lifecycle remains the source of truth for VerifiedFact.
"""

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _id() -> str:
    return str(uuid4())


class HypothesisStatus(StrEnum):
    OPEN = "OPEN"
    VALIDATING = "VALIDATING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ValidationStatus(StrEnum):
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"

    # Compatibility alias for older callers.  It deliberately serializes to
    # VALIDATED, so the persisted security vocabulary has one successful
    # validation status rather than SUCCESS and VALIDATED as two values.
    SUCCESS = "VALIDATED"


# ValidationEvidence remains a compatibility-shaped input object for the
# cross-vulnerability adapters.  Its status vocabulary is the same canonical
# ValidationStatus used by ValidationResult.
ValidationEvidenceStatus = ValidationStatus


class ExploitStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class FindingStatus(StrEnum):
    CREATED = "CREATED"
    REJECTED = "REJECTED"


class SecurityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class HypothesisLocation(SecurityModel):
    url: str = ""
    parameter: str = ""


class ValidationControls(SecurityModel):
    baseline: bool = False
    positive_control: bool = False
    negative_control: bool = False


class Reproduction(SecurityModel):
    repeat_count: int = Field(default=0, ge=0)
    stable: bool = False


class VulnerabilityHypothesis(SecurityModel):
    id: str = Field(default_factory=_id)
    type: str
    location: HypothesisLocation = Field(default_factory=HypothesisLocation)
    source_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: HypothesisStatus = HypothesisStatus.OPEN
    validation_requirements: list[str] = Field(default_factory=list)


class ValidationResult(SecurityModel):
    id: str = Field(default_factory=_id)
    hypothesis_id: str
    type: str = "SQL_INJECTION_VALIDATION"
    status: ValidationStatus
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    controls: ValidationControls = Field(default_factory=ValidationControls)
    reproduction: Reproduction = Field(default_factory=Reproduction)


class ValidationEvidence(SecurityModel):
    """Uniform cross-vulnerability validation result."""

    vulnerability_type: str
    target: str = ""
    parameter: str = ""
    request: Any = Field(default_factory=dict)
    response: Any = Field(default_factory=dict)
    control_group: Any = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ValidationEvidenceStatus


class ExploitScope(SecurityModel):
    type: str = ""
    data_fields: list[str] = Field(default_factory=list)


class ExploitResult(SecurityModel):
    id: str = Field(default_factory=_id)
    validation_id: str
    status: ExploitStatus
    evidence_ids: list[str] = Field(default_factory=list)
    scope: ExploitScope = Field(default_factory=ExploitScope)


class ImpactAssessment(SecurityModel):
    id: str = Field(default_factory=_id)
    exploit_id: str
    impact_type: str
    severity: str
    evidence_ids: list[str] = Field(default_factory=list)
    business_impact: str


class SecurityFinding(SecurityModel):
    id: str = Field(default_factory=_id)
    vulnerability_type: str
    status: FindingStatus
    hypothesis_id: str
    validation_id: str
    exploit_id: str
    impact_id: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class InformationEvidence(SecurityModel):
    id: str = Field(default_factory=_id)
    fact_id: str
    fact_key: str
    value: Any = None
    evidence_ids: list[str] = Field(default_factory=list)
    category: str = "ENVIRONMENT_INFORMATION"
