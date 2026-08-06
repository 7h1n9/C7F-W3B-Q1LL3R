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


@pytest.fixture
async def run_context(session_factory):
    async with session_factory() as session:
        challenge = Challenge(
            name="strategy-v2",
            target_url="http://asset.local",
            allowed_hosts=["asset.local"],
            challenge_type="WEB_TARGET",
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=".",
            role_snapshot_json={},
            status="PLANNING",
        )
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, "WEB_TARGET", [], "strategy-v2", "")
        yield session, run


def _boolean_args(true_condition="' AND 1=1 -- ", false_condition="' AND 1=2 -- "):
    return {
        "request": {
            "method": "POST",
            "url": "http://asset.local/api/warranty/check",
            "json": {"asset_no": "PC-2026-013", "department": "OPS"},
        },
        "test_field": "asset_no",
        "baseline_value": "PC-2026-013",
        "control_fields": {"department": "OPS"},
        "true_condition": true_condition,
        "false_condition": false_condition,
        "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
    }


def _strategy(variant, *, family="BOOLEAN"):
    return {
        "vulnerability_type": "SQL_INJECTION",
        "strategy_family": family,
        "strategy_variant": variant,
        "signal_type": "RESPONSE_DIFFERENTIAL",
        "encoding": "PLAIN",
    }


async def _reserve(session, run, args, strategy):
    return await experiment_strategy_manager.reserve(
        session,
        run,
        tool_name="sql_boolean_compare",
        stage="BOOLEAN_ORACLE",
        arguments=args,
        independent_variable="asset_no",
        hypothesis="test SQL injection strategy",
        strategy_metadata=strategy,
    )


@pytest.mark.asyncio
async def test_same_execution_identity_is_rejected(run_context):
    session, run = run_context
    args = _boolean_args()
    first, first_record = await _reserve(session, run, args, _strategy("AND"))
    second, second_record = await _reserve(session, run, args, _strategy("AND"))

    assert first is True
    assert second is False
    assert first_record["execution_fingerprint"] == second_record["execution_fingerprint"]


@pytest.mark.asyncio
async def test_and_and_or_strategy_identities_do_not_conflict(run_context):
    session, run = run_context
    and_args = _boolean_args()
    or_args = _boolean_args("' OR 1=1 -- ", "' OR 1=2 -- ")
    first, first_record = await _reserve(session, run, and_args, _strategy("AND"))
    second, second_record = await _reserve(session, run, or_args, _strategy("OR"))

    assert first is True and second is True
    assert first_record["strategy_fingerprint"] != second_record["strategy_fingerprint"]
    assert first_record["execution_fingerprint"] != second_record["execution_fingerprint"]


@pytest.mark.asyncio
async def test_failed_execution_allows_one_recovery_retry(run_context):
    session, run = run_context
    args = _boolean_args()
    first, first_record = await _reserve(session, run, args, _strategy("AND"))
    await experiment_strategy_manager.record_result(
        session, run, first_record["experiment_id"], result="FAILED", failure_reason="RUNNER_TIMEOUT"
    )
    retry, retry_record = await _reserve(session, run, args, _strategy("AND"))

    assert first is True
    assert retry is True
    assert retry_record["attempt_count"] == 2


@pytest.mark.asyncio
async def test_inconclusive_experiment_allows_strategy_migration(run_context):
    session, run = run_context
    first, first_record = await _reserve(session, run, _boolean_args(), _strategy("AND"))
    await experiment_strategy_manager.record_result(
        session, run, first_record["experiment_id"], result="INCONCLUSIVE", failure_reason="NO_DIFFERENCE"
    )
    migrated, migrated_record = await _reserve(
        session,
        run,
        _boolean_args("' OR 1=1 -- ", "' OR 1=2 -- "),
        _strategy("OR"),
    )

    assert first is True
    assert migrated is True
    assert migrated_record["strategy_variant"] == "OR"


@pytest.mark.asyncio
async def test_confirmed_strategy_blocks_same_strategy(run_context):
    session, run = run_context
    first, first_record = await _reserve(session, run, _boolean_args(), _strategy("AND"))
    await experiment_strategy_manager.record_result(
        session, run, first_record["experiment_id"], result="CONFIRMED"
    )
    same_strategy_new_payload, _ = await _reserve(
        session,
        run,
        _boolean_args("' AND 2=2 -- ", "' AND 2=3 -- "),
        _strategy("AND"),
    )

    assert first is True
    assert same_strategy_new_payload is False


@pytest.mark.asyncio
async def test_boolean_strategy_budget_requires_migration(run_context):
    session, run = run_context
    variants = ["AND", "OR", "COMMENT"]
    for index, variant in enumerate(variants):
        args = _boolean_args(
            f"' {variant.lower()} {index}=1 -- ",
            f"' {variant.lower()} {index}=2 -- ",
        )
        reserved, record = await _reserve(session, run, args, _strategy(variant))
        assert reserved is True
        await experiment_strategy_manager.record_result(
            session, run, record["experiment_id"], result="INCONCLUSIVE", failure_reason="NO_SIGNAL"
        )

    exhausted, _ = await _reserve(
        session,
        run,
        _boolean_args("' whitespace 4=1 -- ", "' whitespace 4=2 -- "),
        _strategy("WHITESPACE"),
    )
    assert exhausted is False
