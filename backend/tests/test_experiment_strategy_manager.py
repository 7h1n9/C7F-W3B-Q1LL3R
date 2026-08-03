import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.services.experiment_strategy_manager import experiment_strategy_manager
from app.services.solver_state import solver_state_service


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_identical_http_experiment_is_reserved_once(session_factory):
    async with session_factory() as session:
        challenge = Challenge(name="asset", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, status="PLANNING")
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, "WEB_TARGET", [], "asset", "asset")
        args = {"method": "POST", "url": "/api/warranty/check", "json": {"asset_no": "PC-2026-013", "department": "OPS"}}
        first, first_record = await experiment_strategy_manager.reserve(session, run, tool_name="http_request", stage="BUSINESS_BASELINE", arguments=args, independent_variable="", hypothesis="valid baseline")
        second, second_record = await experiment_strategy_manager.reserve(session, run, tool_name="http_request", stage="BUSINESS_BASELINE", arguments=args, independent_variable="", hypothesis="valid baseline")
        assert first is True
        assert second is False
        assert first_record["experiment_id"] == second_record["experiment_id"]
        assert second_record["result"] == "RESERVED"


@pytest.mark.asyncio
async def test_same_request_cannot_evade_guard_by_changing_hypothesis(session_factory):
    async with session_factory() as session:
        challenge = Challenge(name="asset", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, status="PLANNING")
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, "WEB_TARGET", [], "asset", "asset")
        args = {"method": "POST", "url": "/api/warranty/check", "json": {"asset_no": "PC-2026-013", "department": "OPS"}}
        first, _ = await experiment_strategy_manager.reserve(session, run, tool_name="http_request", stage="BUSINESS_BASELINE", arguments=args, independent_variable="", hypothesis="hypothesis one")
        second, _ = await experiment_strategy_manager.reserve(session, run, tool_name="http_request", stage="BUSINESS_BASELINE", arguments=args, independent_variable="", hypothesis="hypothesis two")
        assert first is True
        assert second is False


def test_changed_stage_or_hypothesis_creates_new_experiment():
    args = {"method": "POST", "url": "/api/warranty/check", "json": {"asset_no": "PC-2026-013", "department": "OPS"}}
    baseline = experiment_strategy_manager.record(tool_name="http_request", stage="BUSINESS_BASELINE", arguments=args, independent_variable="", hypothesis="valid baseline")
    invalid = experiment_strategy_manager.record(tool_name="http_request", stage="BUSINESS_BASELINE", arguments={**args, "json": {"asset_no": "PC-2026-013", "department": "FIN"}}, independent_variable="department", hypothesis="invalid baseline")
    oracle = experiment_strategy_manager.record(tool_name="sql_boolean_compare", stage="BOOLEAN_ORACLE", arguments={"test_field": "department"}, independent_variable="department", hypothesis="department controls predicate")
    assert len({baseline["experiment_id"], invalid["experiment_id"], oracle["experiment_id"]}) == 3
