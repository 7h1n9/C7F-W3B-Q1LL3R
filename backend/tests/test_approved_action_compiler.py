import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import DomainError
from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AnalysisReview, PlannerProposal
from app.models.run import SolveRun
from app.models.solver_state import SolverState
from app.schemas.multi_agent import CompiledApprovedAction
from app.services.approved_action_compiler import approved_action_compiler
from app.tools.registry import load_tool_definitions


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _rows(session, *, tool: str, arguments: dict, state: dict | None = None):
    challenge = Challenge(
        name="Warranty challenge",
        target_url="http://192.168.236.1:28036",
        allowed_hosts=["192.168.236.1"],
        metadata_json={"adapter": "asset_warranty", "endpoint": "/api/warranty/check", "method": "POST", "content_type": "application/json", "fields": ["asset_no", "department"], "control_values": {"asset_no": "PC-2026-013", "department": "OPS"}},
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(challenge_id=challenge.id, workspace_path=".")
    session.add(run)
    await session.flush()
    proposal = PlannerProposal(run_id=run.id, proposal_id="PP-1", current_stage="BASELINE", next_agent="EXPLOIT", objective="bounded", allowed_tools_json=[tool], budget_json={"max_logical_calls": 1, "max_internal_requests": 8, "max_runtime_seconds": 300}, success_condition="one result")
    session.add(proposal)
    await session.flush()
    review = AnalysisReview(proposal_id=proposal.id, task_kind="PLAN_REVIEW", decision="APPROVE", question_being_tested="bounded", recommended_tool=tool, required_controls_json={}, expected_true_signal_json={"ok": True}, expected_false_signal_json={"ok": False}, approved_arguments_json=arguments)
    session.add(review)
    session.add(SolverState(run_id=run.id, current_phase="BASELINE", capability_ledger_json=state or {}))
    await session.flush()
    return run, challenge, proposal, review


@pytest.mark.asyncio
async def test_http_request_compiles_metadata_endpoint_and_json(session_factory):
    async with session_factory() as session:
        run, challenge, proposal, review = await _rows(session, tool="http_request", arguments={"path": "/api/warranty/check", "method": "POST", "json": {"asset_no": "PC-2026-013", "department": "OPS"}})
        compiled = await approved_action_compiler.compile(session, run, challenge, proposal, review, "http_request")
        assert isinstance(compiled, CompiledApprovedAction)
        assert compiled.arguments == {"method": "POST", "url": "http://192.168.236.1:28036/api/warranty/check", "headers": {"Content-Type": "application/json"}, "json": {"asset_no": "PC-2026-013", "department": "OPS"}}
        assert load_tool_definitions()["http_request"].validate_arguments(compiled.arguments) == compiled.arguments


@pytest.mark.asyncio
async def test_http_request_compiles_body_string_without_resetting_to_metadata_controls(session_factory):
    async with session_factory() as session:
        run, challenge, proposal, review = await _rows(
            session,
            tool="http_request",
            arguments={
                "path": "/api/warranty/check",
                "method": "POST",
                "body": '{"asset_no":"PC-2026-013-NOTFOUND","department":"OPS"}',
            },
        )
        compiled = await approved_action_compiler.compile(session, run, challenge, proposal, review, "http_request")
        assert compiled.arguments["json"] == {"asset_no": "PC-2026-013-NOTFOUND", "department": "OPS"}
        assert load_tool_definitions()["http_request"].validate_arguments(compiled.arguments) == compiled.arguments


@pytest.mark.asyncio
async def test_boolean_compare_compiler_emits_runner_contract_without_task_budget(session_factory):
    async with session_factory() as session:
        run, challenge, proposal, review = await _rows(session, tool="sql_boolean_compare", arguments={"test_field": "department", "max_requests": 1})
        compiled = await approved_action_compiler.compile(session, run, challenge, proposal, review, "sql_boolean_compare")
        assert compiled.arguments["request"]["url"].endswith("/api/warranty/check")
        assert compiled.arguments["test_field"] == "department"
        assert compiled.arguments["max_requests"] == 5
        assert compiled.arguments["oracle"] == {"json_field": "matched", "true_value": True, "false_value": False}
        assert "max_logical_calls" not in compiled.arguments
        assert load_tool_definitions()["sql_boolean_compare"].validate_arguments(compiled.arguments) == compiled.arguments


@pytest.mark.asyncio
async def test_sqlite_metadata_requires_verified_boolean_oracle_source(session_factory):
    async with session_factory() as session:
        run, challenge, proposal, review = await _rows(session, tool="sqlite_metadata_discovery", arguments={"target_expression": "SELECT name FROM sqlite_master"})
        with pytest.raises(DomainError) as error:
            await approved_action_compiler.compile(session, run, challenge, proposal, review, "sqlite_metadata_discovery")
        assert error.value.code == "APPROVED_ACTION_COMPILE_FAILED"
        assert error.value.details["reason"] == "BOOLEAN_ORACLE_REQUIRED"
