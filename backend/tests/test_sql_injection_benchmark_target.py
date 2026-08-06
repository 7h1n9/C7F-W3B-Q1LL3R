import httpx
import pytest

from app.benchmark_targets.sql_injection.app import create_app
from app.security.schemas import (
    ExploitResult,
    ExploitStatus,
    ImpactAssessment,
    SecurityFinding,
    ValidationResult,
    ValidationStatus,
    VulnerabilityHypothesis,
)
from app.security.service import security_finding_service


@pytest.mark.asyncio
async def test_golden_target_has_stable_oracle_and_extraction(tmp_path):
    application = create_app(tmp_path / "golden.db")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://golden.test") as client:
        baseline = await client.get("/search", params={"asset_no": "PC-001"})
        true_response = await client.get("/search", params={"asset_no": "PC-001' AND 1=1-- "})
        false_response = await client.get("/search", params={"asset_no": "PC-001' AND 1=2-- "})
        exploit_response = await client.get(
            "/search",
            params={"asset_no": "PC-001' UNION SELECT secret FROM assets-- "},
        )

    assert baseline.json() == {"matched": True}
    assert true_response.json()["matched"] is True
    assert false_response.json()["matched"] is False
    assert exploit_response.json()["extracted_data"] == [
        "FLAG{GOLDEN_PATH_SQL_INJECTION}",
        "asset-secret-002",
    ]


def test_existing_security_services_build_complete_golden_lifecycle():
    hypothesis = VulnerabilityHypothesis(
        type="SQL_INJECTION",
        confidence=0.99,
        location={"url": "http://golden.test/search", "parameter": "asset_no"},
    )
    validation = ValidationResult(
        hypothesis_id=hypothesis.id,
        type="SQL_INJECTION_VALIDATION",
        status=ValidationStatus.SUCCESS,
        evidence_ids=["validation-evidence"],
        confidence=0.99,
        controls={"baseline": True, "positive_control": True, "negative_control": True},
        reproduction={"repeat_count": 3, "stable": True},
    )
    exploit = ExploitResult(
        validation_id=validation.id,
        status=ExploitStatus.SUCCESS,
        evidence_ids=["exploit-evidence"],
        scope={"type": "BUSINESS_DATA", "data_fields": ["assets.secret"]},
    )
    impact = ImpactAssessment(
        exploit_id=exploit.id,
        impact_type="database_data_disclosure",
        severity="HIGH",
        evidence_ids=["impact-evidence"],
        business_impact="Unauthorized disclosure of asset secrets.",
    )

    finding = security_finding_service.create_finding(hypothesis, validation, exploit, impact)

    assert isinstance(finding, SecurityFinding)
    assert finding.status == "CREATED"
    assert finding.vulnerability_type == "SQL_INJECTION"
