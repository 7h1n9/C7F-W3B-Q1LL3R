import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import VerifiedFact
from app.models.run import SolveRun
from app.models.run import RunEvent
from app.models.solver_state import SolverState
from app.security.schemas import (
    ExploitResult,
    ExploitStatus,
    ImpactAssessment,
    ValidationControls,
    ValidationResult,
    ValidationStatus,
    VulnerabilityHypothesis,
)
from app.security.decision import security_decision_engine
from app.security.service import security_finding_service
from app.services.solver_state import solver_state_service
from app.orchestration.multi_agent_orchestrator import MultiAgentOrchestrator


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _run(session) -> SolveRun:
    challenge = Challenge(
        name="security reasoning",
        target_url="http://target.local",
        allowed_hosts=["target.local"],
        challenge_type="WEB_TARGET",
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(challenge_id=challenge.id, workspace_path=".")
    session.add(run)
    await session.flush()
    return run


def test_environment_information_cannot_become_security_finding():
    facts = [
        VerifiedFact(
            id="fact-version",
            run_id="run-1",
            fact_key="mysql_version",
            fact_type="MYSQL_METADATA",
            value_json="8.4",
            evidence_ids_json=["e-version"],
            promotion_status="VERIFIED",
        ),
        VerifiedFact(
            id="fact-db",
            run_id="run-1",
            fact_key="DATABASE()",
            fact_type="MYSQL_METADATA",
            value_json="asset_warranty",
            evidence_ids_json=["e-db"],
            promotion_status="VERIFIED",
        ),
    ]

    mapped = [security_finding_service.map_verified_fact(fact) for fact in facts]

    assert all(item is not None for item in mapped)
    assert all(item.__class__.__name__ == "InformationEvidence" for item in mapped)
    assert not any(item.__class__.__name__ == "SecurityFinding" for item in mapped)


def test_mysql_boolean_oracle_maps_to_successful_validation_result():
    fact = VerifiedFact(
        id="fact-oracle",
        run_id="run-1",
        fact_key="asset_warranty.mysql_boolean_oracle",
        fact_type="BOOLEAN_ORACLE",
        value_json={"hypothesis_id": "hyp-sqli"},
        confidence=95,
        evidence_ids_json=["e-oracle"],
        promotion_status="VERIFIED",
    )

    mapped = security_finding_service.map_verified_fact(fact)

    assert mapped is not None
    assert mapped.type == "SQL_INJECTION_VALIDATION"
    assert mapped.status == ValidationStatus.SUCCESS
    assert mapped.evidence_ids == ["e-oracle"]


def test_generic_sql_validation_fact_maps_to_validated_result():
    fact = VerifiedFact(
        id="fact-golden-validation",
        run_id="run-1",
        fact_key="security.sql_injection.validation",
        fact_type="SECURITY_VALIDATION",
        value_json={
            "vulnerability_type": "SQL_INJECTION",
            "status": "VALIDATED",
            "confidence": 0.95,
            "evidence_ids": ["e-validation"],
            "controls": {"baseline": True, "positive_control": True, "negative_control": True},
            "reproduction": {"repeat_count": 5, "stable": True},
        },
        confidence=95,
        evidence_ids_json=["e-validation"],
        promotion_status="VERIFIED",
    )

    mapped = security_finding_service.map_verified_fact(fact)

    assert mapped is not None
    assert isinstance(mapped, ValidationResult)
    assert mapped.status == ValidationStatus.VALIDATED
    assert mapped.confidence == 0.95
    assert mapped.evidence_ids == ["e-validation"]


@pytest.mark.asyncio
async def test_verified_generic_validation_updates_security_context_and_events(session_factory):
    async with session_factory() as session:
        run = await _run(session)
        state = SolverState(run_id=run.id)
        session.add(state)
        fact = VerifiedFact(
            id="fact-context-validation",
            run_id=run.id,
            fact_key="security.sql_injection.validation",
            fact_type="SECURITY_VALIDATION",
            value_json={"status": "VALIDATED", "confidence": 0.95, "evidence_ids": ["e-context"]},
            confidence=95,
            evidence_ids_json=["e-context"],
            promotion_status="VERIFIED",
        )
        session.add(fact)
        await session.flush()

        await MultiAgentOrchestrator()._record_security_mapping(session, run, fact)

        loaded = await solver_state_service.load(session, run.id)
        assert loaded.security_context_json["validation_results"][0]["status"] == "VALIDATED"
        assert loaded.security_context_json["validation_results"][0]["confidence"] == 0.95
        events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.sequence))).all())
        assert [event.event_type for event in events] == [
            "validation.created",
            "security.context.updated",
            "attack.state.updated",
            "attack.transition.created",
        ]


def test_business_baseline_does_not_map_to_vulnerability_semantics():
    fact = VerifiedFact(
        id="fact-baseline",
        run_id="run-1",
        fact_key="asset_warranty.valid_baseline",
        fact_type="BUSINESS_RESPONSE_BASELINE",
        value_json={"status_code": 200},
        evidence_ids_json=["e-baseline"],
        promotion_status="VERIFIED",
    )

    assert security_finding_service.map_verified_fact(fact) is None


def test_information_evidence_does_not_allow_planner_completion():
    guidance = security_finding_service.planner_guidance({
        "information_evidence": [
            {"fact_key": "DATABASE()", "value": "asset_warranty"},
            {"fact_key": "VERSION()", "value": "8.4"},
            {"fact_key": "information_schema.tables", "value": ["assets"]},
        ],
    })

    assert guidance["next_action"] == "HYPOTHESIS_VALIDATION"
    assert guidance["completion_allowed"] is False


def test_successful_sql_validation_requires_exploit_and_impact():
    guidance = security_finding_service.planner_guidance({
        "validation_results": [{"type": "SQL_INJECTION_VALIDATION", "status": "SUCCESS"}],
    })

    assert guidance["next_action"] == "EXPLOIT_IMPACT_CONFIRMATION"
    assert guidance["completion_allowed"] is False


def test_created_security_finding_allows_planner_reporting():
    guidance = security_finding_service.planner_guidance({
        "findings": [{"status": "CREATED", "vulnerability_type": "SQL_INJECTION"}],
    })

    assert guidance["next_action"] == "REPORTING"
    assert guidance["completion_allowed"] is True


def test_information_evidence_cannot_enter_completion_phase():
    decision = security_decision_engine.decide({
        "information_evidence": [
            {"fact_key": "DATABASE()"},
            {"fact_key": "VERSION()"},
        ],
    })

    assert decision is not None
    assert decision.required_phase == "HYPOTHESIS"
    assert decision.reporting_allowed is False


def test_successful_sql_validation_requires_exploitation_phase():
    decision = security_decision_engine.decide({
        "validation_results": [{"type": "SQL_INJECTION_VALIDATION", "status": "SUCCESS"}],
    })

    assert decision is not None
    assert decision.required_phase == "EXPLOITATION"
    assert decision.reporting_allowed is False


def test_created_security_finding_enters_reporting_phase():
    decision = security_decision_engine.decide({
        "findings": [{"status": "CREATED", "vulnerability_type": "SQL_INJECTION"}],
    })

    assert decision is not None
    assert decision.required_phase == "REPORTING"
    assert decision.reporting_allowed is True


def test_controller_facing_evaluate_alias_returns_security_decision():
    decision = security_decision_engine.evaluate({
        "validation_results": [{"status": "SUCCESS"}],
    })

    assert decision is not None
    assert decision.required_phase == "EXPLOITATION"
    assert decision.confidence == 1.0


def test_sql_injection_complete_chain_creates_finding():
    hypothesis = VulnerabilityHypothesis(
        type="SQL_INJECTION",
        confidence=0.9,
        source_evidence_ids=["e-attack-surface"],
        validation_requirements=["baseline", "positive_control", "negative_control"],
    )
    validation = ValidationResult(
        hypothesis_id=hypothesis.id,
        status=ValidationStatus.SUCCESS,
        evidence_ids=["e-baseline", "e-positive", "e-negative"],
        confidence=0.85,
        controls=ValidationControls(baseline=True, positive_control=True, negative_control=True),
    )
    exploit = ExploitResult(
        validation_id=validation.id,
        status=ExploitStatus.SUCCESS,
        evidence_ids=["e-business-data"],
        scope={"type": "UNAUTHORIZED_BUSINESS_DATA_READ", "data_fields": ["asset_id", "owner_name"]},
    )
    impact = ImpactAssessment(
        exploit_id=exploit.id,
        impact_type="UNAUTHORIZED_DATA_READ",
        severity="HIGH",
        evidence_ids=["e-business-data"],
        business_impact="Unauthenticated users can read business asset records.",
    )

    finding = security_finding_service.create_finding(hypothesis, validation, exploit, impact)

    assert finding is not None
    assert finding.status == "CREATED"
    assert finding.vulnerability_type == "SQL_INJECTION"
    assert finding.impact_id == impact.id


def test_exception_without_controls_is_inconclusive_and_cannot_create_finding():
    hypothesis = VulnerabilityHypothesis(type="SQL_INJECTION", confidence=0.8)
    validation = ValidationResult(
        hypothesis_id=hypothesis.id,
        status=ValidationStatus.INCONCLUSIVE,
        evidence_ids=["e-exception"],
    )
    exploit = ExploitResult(
        validation_id=validation.id,
        status=ExploitStatus.SUCCESS,
        evidence_ids=["e-exception"],
    )
    impact = ImpactAssessment(
        exploit_id=exploit.id,
        impact_type="UNAUTHORIZED_DATA_READ",
        severity="HIGH",
        evidence_ids=["e-exception"],
        business_impact="Unproven impact.",
    )

    assert validation.status == ValidationStatus.INCONCLUSIVE
    assert security_finding_service.create_finding(hypothesis, validation, exploit, impact) is None


@pytest.mark.asyncio
async def test_security_blackboard_is_compatible_with_existing_solver_state(session_factory):
    async with session_factory() as session:
        run = await _run(session)
        state = SolverState(
            run_id=run.id,
            confirmed_facts_json=[{"fact": "kept"}],
            capability_ledger_json={"legacy_capability": True},
        )
        session.add(state)
        await session.flush()

        await solver_state_service.append_security_object(
            session, run.id, "hypotheses", {"id": "h-1", "type": "SQL_INJECTION"}
        )
        await session.commit()
        loaded = await solver_state_service.load(session, run.id)

        assert loaded.security_context_json["hypotheses"] == [{"id": "h-1", "type": "SQL_INJECTION"}]
        assert loaded.confirmed_facts_json == [{"fact": "kept"}]
        assert loaded.capability_ledger_json == {"legacy_capability": True}
