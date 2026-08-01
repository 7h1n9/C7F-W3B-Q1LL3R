from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import DomainError
from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import EvidenceLedger, VerifiedFact
from app.models.run import Artifact, SolveRun, ToolCall
from app.models.solver_state import SolverState
from app.orchestration.multi_agent_orchestrator import MultiAgentOrchestrator
from app.orchestration.state_machine import RunStatus
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskContract,
    AgentTaskResultContract,
    AnalysisDecision,
    AnalysisReviewContract,
    EvidenceLedgerContract,
    PlannerProposalContract,
    TaskBudget,
)
from app.services.multi_agent import deterministic_controller


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _run(session) -> SolveRun:
    challenge = Challenge(name="multi", target_url="http://multi.local", allowed_hosts=["multi.local"], challenge_type="WEB_TARGET")
    session.add(challenge)
    await session.flush()
    run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, solver_mode="multi_agent_v1")
    session.add(run)
    await session.flush()
    return run


async def _asset_mysql_run(session) -> tuple[Challenge, SolveRun]:
    challenge = Challenge(
        name="asset warranty",
        target_url="http://asset.local/api/warranty",
        allowed_hosts=["asset.local"],
        challenge_type="WEB_TARGET",
        metadata_json={"adapter": "asset_warranty", "dbms": "mysql"},
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, solver_mode="multi_agent_v1")
    session.add(run)
    await session.flush()
    session.add(SolverState(run_id=run.id, current_phase="CHAINING", capability_ledger_json={"mysql_boolean_oracle_confirmed": {"confirmed": True}}))
    await session.flush()
    return challenge, run


@pytest.mark.asyncio
async def test_task_lease_and_promotion_gate(session_factory) -> None:
    async with session_factory() as session:
        run = await _run(session)
        await deterministic_controller.seed_policies(session)
        task = await deterministic_controller.create_task(
            session,
            AgentTaskContract(
                task_id="AT-RECON-001", run_id=run.id, agent_role=AgentRole.RECON,
                objective="Discover the bounded HTTP surface", allowed_tools=["http_request"],
                budget=TaskBudget(max_logical_calls=1, max_internal_requests=8, max_runtime_seconds=120),
            ),
        )
        token = await deterministic_controller.claim_task(session, task.id, "worker-a")
        with pytest.raises(DomainError, match="leased"):
            await deterministic_controller.claim_task(session, task.id, "worker-b")
        no_value = await deterministic_controller.complete_task(
            session, task.id, AgentTaskResultContract(task_id=task.id, status="COMPLETED"), token
        )
        assert no_value.status == "NO_VALUE"


@pytest.mark.asyncio
async def test_evidence_chain_and_candidate_fact_are_controller_owned(session_factory, tmp_path: Path) -> None:
    async with session_factory() as session:
        run = await _run(session)
        await deterministic_controller.seed_policies(session)
        task = await deterministic_controller.create_task(
            session,
            AgentTaskContract(task_id="AT-ANALYSIS-001", run_id=run.id, agent_role=AgentRole.ANALYSIS, objective="Test one hypothesis", allowed_tools=["http_compare"]),
        )
        token = await deterministic_controller.claim_task(session, task.id, "analysis-worker")
        tool_call = ToolCall(run_id=run.id, tool_name="http_compare", arguments_json={})
        session.add(tool_call)
        await session.flush()
        artifact_path = tmp_path / "response.txt"
        artifact_path.write_text("matched=false", encoding="utf-8")
        artifact = Artifact(run_id=run.id, tool_call_id=tool_call.id, artifact_type="HTTP_RESPONSE", file_path=str(artifact_path), sha256="a" * 64)
        session.add(artifact)
        await session.flush()
        evidence = await deterministic_controller.evidence.record(
            session,
            EvidenceLedgerContract(
                evidence_id="E-001", run_id=run.id, evidence_type="HTTP_RESPONSE", artifact_id=artifact.id,
                tool_call_id=tool_call.id, agent_task_id=task.id, summary="controlled response",
                source_chain=[artifact.id, tool_call.id, task.id],
            ),
        )
        decision = await deterministic_controller.complete_task(
            session,
            task.id,
            AgentTaskResultContract(
                task_id=task.id, status="COMPLETED", evidence_ids=[evidence.id],
                new_facts=[{"fact_key": "endpoint:/api/check", "fact_type": "HTTP_ENDPOINT", "value": {"method": "POST"}, "confidence": 90}],
            ),
            token,
        )
        assert decision.status == "CANDIDATE"
        fact = await session.scalar(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.fact_key == "endpoint:/api/check"))
        assert fact is not None and fact.promotion_status == "CANDIDATE"
        assert (await session.scalar(select(EvidenceLedger).where(EvidenceLedger.id == evidence.id))).source_chain == [artifact.id, tool_call.id, task.id]


@pytest.mark.asyncio
async def test_verify_isolated_task_is_the_only_terminal_promotion_path(session_factory) -> None:
    async with session_factory() as session:
        run = await _run(session)
        run.status = RunStatus.VERIFYING_FLAG.value
        run.current_phase = RunStatus.VERIFYING_FLAG.value
        await deterministic_controller.seed_policies(session)
        task = await deterministic_controller.create_task(
            session,
            AgentTaskContract(task_id="AT-VERIFY-001", run_id=run.id, agent_role=AgentRole.VERIFY, objective="Freshly reproduce the candidate", allowed_tools=["http_request"]),
        )
        token = await deterministic_controller.claim_task(session, task.id, "verify-worker")
        tool_call = ToolCall(run_id=run.id, tool_name="http_request", arguments_json={})
        session.add(tool_call)
        await session.flush()
        artifact = Artifact(run_id=run.id, tool_call_id=tool_call.id, artifact_type="FLAG_RESPONSE", file_path="responses/flag.txt")
        session.add(artifact)
        await session.flush()
        evidence = await deterministic_controller.evidence.record(
            session,
            EvidenceLedgerContract(evidence_id="E-FRESH-001", run_id=run.id, evidence_type="FRESH_REPRODUCTION", artifact_id=artifact.id, tool_call_id=tool_call.id, agent_task_id=task.id, summary="fresh flag response"),
        )
        await deterministic_controller.complete_task(session, task.id, AgentTaskResultContract(task_id=task.id, status="COMPLETED", evidence_ids=[evidence.id]), token)
        candidate = await deterministic_controller.finalize_verified_candidate(session, run, candidate="flag{fresh}", verify_task_id=task.id, source_artifact_id=artifact.id, producing_tool_call_id=tool_call.id, evidence_ids=[evidence.id], pattern_matched=True, fresh_reproduction=True)
        assert candidate.verified is True
        assert run.status == RunStatus.COMPLETED_SOLVED.value


@pytest.mark.asyncio
async def test_asset_warranty_verified_baselines_advance_capability_ledger(session_factory) -> None:
    async with session_factory() as session:
        run = await _run(session)
        state = SolverState(run_id=run.id, current_phase="BASELINE", capability_ledger_json={})
        session.add(state)
        valid = VerifiedFact(
            run_id=run.id,
            fact_key="asset_warranty.valid_baseline",
            fact_type="BUSINESS_RESPONSE_BASELINE",
            value_json={"response_signature": {"matched": True}},
            confidence=90,
            evidence_ids_json=["E-valid"],
            promotion_status="VERIFIED",
        )
        invalid = VerifiedFact(
            run_id=run.id,
            fact_key="asset_warranty.invalid_baseline",
            fact_type="BUSINESS_RESPONSE_BASELINE",
            value_json={"response_signature": {"matched": False}},
            confidence=90,
            evidence_ids_json=["E-invalid"],
            promotion_status="VERIFIED",
        )
        session.add_all([valid, invalid])
        await session.flush()
        orchestrator = MultiAgentOrchestrator()

        await orchestrator._record_verified_fact_capabilities(session, run, valid)
        await orchestrator._record_verified_fact_capabilities(session, run, invalid)

        await session.refresh(state)
        assert "warranty_endpoint_identified" in state.capability_ledger_json
        assert "request_contract_confirmed" in state.capability_ledger_json
        assert "valid_business_baseline_confirmed" in state.capability_ledger_json
        assert "invalid_business_baseline_confirmed" in state.capability_ledger_json
        assert "business_response_differential_confirmed" in state.capability_ledger_json


@pytest.mark.asyncio
async def test_asset_warranty_current_database_does_not_satisfy_finish_gate(session_factory) -> None:
    async with session_factory() as session:
        challenge, run = await _asset_mysql_run(session)
        session.add_all([
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.valid_baseline", fact_type="BUSINESS_RESPONSE_BASELINE", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.invalid_baseline", fact_type="BUSINESS_RESPONSE_BASELINE", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_boolean_oracle", fact_type="BOOLEAN_ORACLE", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_dbms", fact_type="MYSQL_DBMS", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_version", fact_type="MYSQL_VERSION", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_version_comment", fact_type="MYSQL_VERSION_COMMENT", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.current_database", fact_type="CURRENT_DATABASE", value_json={"database": "asset_warranty"}, confidence=95, promotion_status="VERIFIED"),
        ])
        await session.flush()
        orchestrator = MultiAgentOrchestrator()

        assert await orchestrator._asset_warranty_mysql_finish_ready(session, run, challenge) is False
        assert orchestrator._max_replan_cycles(run, challenge) >= 12

        plan = await orchestrator._mysql_metadata_plan(session, run)
        assert plan["target_expression"] == "information_schema.tables"
        assert plan["stage"] == "tables"


@pytest.mark.asyncio
async def test_asset_warranty_metadata_finish_gate_requires_tables_columns_and_ledger(session_factory) -> None:
    async with session_factory() as session:
        challenge, run = await _asset_mysql_run(session)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        assert state is not None
        state.capability_ledger_json = {**state.capability_ledger_json, "mysql_metadata_discovered": {"confirmed": True}}
        session.add_all([
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.valid_baseline", fact_type="BUSINESS_RESPONSE_BASELINE", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.invalid_baseline", fact_type="BUSINESS_RESPONSE_BASELINE", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_boolean_oracle", fact_type="BOOLEAN_ORACLE", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_dbms", fact_type="MYSQL_DBMS", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_version", fact_type="MYSQL_VERSION", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_version_comment", fact_type="MYSQL_VERSION_COMMENT", value_json={}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.current_database", fact_type="CURRENT_DATABASE", value_json={"database": "asset_warranty"}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_user_tables", fact_type="MYSQL_USER_TABLES", value_json={"tables": [{"name": "warranties"}]}, confidence=95, promotion_status="VERIFIED"),
            VerifiedFact(run_id=run.id, fact_key="asset_warranty.mysql_candidate_columns", fact_type="MYSQL_CANDIDATE_COLUMNS", value_json={"columns": [{"name": "flag"}]}, confidence=95, promotion_status="VERIFIED"),
        ])
        await session.flush()
        orchestrator = MultiAgentOrchestrator()

        assert await orchestrator._asset_warranty_mysql_finish_ready(session, run, challenge) is True


def test_planner_cannot_schedule_execution_and_analysis_rejects_bad_controls() -> None:
    proposal = PlannerProposalContract(
        proposal_id="PP-001", run_id="R-001", current_stage="ANALYSIS", next_agent=AgentRole.EXPLOIT,
        objective="bounded test", allowed_tools=["sqlmap_run"], success_condition="candidate",
    )
    with pytest.raises(DomainError):
        deterministic_controller.validate_review(
            proposal,
            AnalysisReviewContract(proposal_id="PP-001", decision=AnalysisDecision.APPROVE),
        )
