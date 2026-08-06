import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.services.experiment_result_classifier import ExperimentResultClassifier
from app.services.experiment_strategy_manager import experiment_strategy_manager
from app.services.solver_state import solver_state_service


@pytest.fixture
async def context(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        challenge = Challenge(name="feedback", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="PLANNING")
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, "WEB_TARGET", [], "feedback", "")
        yield session, run
    await engine.dispose()


def _args(variant="AND"):
    operator = variant.lower()
    return {
        "request": {"method": "POST", "url": "http://asset.local/api/check", "json": {"asset_no": "PC-1"}},
        "test_field": "asset_no",
        "true_condition": f"' {operator} 1=1 -- ",
        "false_condition": f"' {operator} 1=2 -- ",
    }


def _strategy(variant="AND", family="BOOLEAN"):
    return {"vulnerability_type": "SQL_INJECTION", "strategy_family": family, "strategy_variant": variant, "signal_type": "RESPONSE_DIFFERENTIAL"}


async def _reserve(session, run, variant="AND", family="BOOLEAN"):
    return await experiment_strategy_manager.reserve(
        session, run, tool_name="sql_boolean_compare", stage="BOOLEAN_ORACLE", arguments=_args(variant),
        independent_variable="asset_no", hypothesis="test SQL strategy", strategy_metadata=_strategy(variant, family),
    )


def _diagnosis(classification):
    return {"classification": classification, "confidence": 0.9, "reason": classification}


@pytest.mark.asyncio
async def test_and_confirmed_cannot_repeat(context):
    session, run = context
    reserved, entry = await _reserve(session, run, "AND")
    assert reserved
    updated = await experiment_strategy_manager.record_result(session, run, entry["experiment_id"], result="COMPLETED", diagnosis={"classification": "ORACLE_CONFIRMED"})
    assert updated["status"] == "CONFIRMED"
    duplicate, _ = await _reserve(session, run, "AND")
    assert duplicate is False


@pytest.mark.asyncio
async def test_and_true_side_failure_migrates_to_or(context):
    session, run = context
    _, entry = await _reserve(session, run, "AND")
    updated = await experiment_strategy_manager.record_result(session, run, entry["experiment_id"], result="COMPLETED", diagnosis=_diagnosis("TRUE_SIDE_FAILED"))
    assert updated["status"] == "INCONCLUSIVE"
    assert updated["next_allowed_actions"] == ["OR"]
    state = await solver_state_service.load(session, run.id)
    assert state.last_experiment_json["strategy_migration"]["to"] == "OR"
    assert state.last_result_classification == "TRUE_SIDE_FAILED"


@pytest.mark.asyncio
async def test_or_no_difference_migrates_to_error_based(context):
    session, run = context
    _, entry = await _reserve(session, run, "OR")
    updated = await experiment_strategy_manager.record_result(session, run, entry["experiment_id"], result="COMPLETED", diagnosis=_diagnosis("NO_DIFFERENCE"))
    assert updated["next_allowed_actions"] == ["ERROR_BASED"]


@pytest.mark.asyncio
async def test_boolean_family_exhaustion_migrates_to_union_or_time(context):
    session, run = context
    for variant in ("AND", "OR", "COMMENT"):
        _, entry = await _reserve(session, run, variant)
        updated = await experiment_strategy_manager.record_result(session, run, entry["experiment_id"], result="COMPLETED", diagnosis=_diagnosis("NO_SIGNAL"))
    assert updated["strategy_migration"]["family_exhausted"] is True
    assert updated["next_allowed_actions"] == ["UNION", "TIME_BASED"]


def test_classifier_derives_boolean_outcome_from_signals():
    classifier = ExperimentResultClassifier()
    result = classifier.classify(
        {"status": "COMPLETED", "stable_true": False, "stable_false": True, "response_differential": False},
        strategy={"strategy_family": "BOOLEAN", "strategy_variant": "AND"},
        explicit_result="COMPLETED",
    )
    assert result["classification"] == "TRUE_SIDE_FAILED"
    assert result["recommended_strategies"] == ["OR"]
