import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.services.solver_state import solver_state_service
from app.services.strategy_continuation import (
    build_strategy_portfolio,
    strategy_continuation_service,
)


def _entry(strategy_family, strategy_variant, experiment_id, result="TRUE_SIDE_FAILED"):
    return {
        "experiment_id": experiment_id,
        "vulnerability_type": "SQL_INJECTION",
        "strategy_family": strategy_family,
        "strategy_variant": strategy_variant,
        "status": "INCONCLUSIVE",
        "result_classification": result,
        "failure_reason": result,
    }


def test_portfolio_excludes_tried_and_keeps_remaining_candidates():
    history = [
        _entry("BOOLEAN", "AND", "e-and"),
        _entry("BOOLEAN", "AND_COMMENT_HASH", "e-hash"),
        _entry("BOOLEAN", "AND_ENCODING", "e-encoding"),
    ]
    portfolio = build_strategy_portfolio(
        history,
        {
            "hypothesis": "Does asset_no participate in a stable predicate?",
            "strategy_migration": {"recommended_strategies": ["UNION", "TIME_BASED"]},
        },
    )

    assert portfolio["tried_strategies"][0]["strategy"] == "BOOLEAN_AND"
    assert portfolio["tried_strategies"][1]["strategy"] == "BOOLEAN_AND_COMMENT_HASH"
    assert portfolio["remaining_strategies"][:2] == ["UNION", "TIME_BASED"]
    assert "BOOLEAN_AND" not in portfolio["next_candidates"]
    assert portfolio["search_exhausted"] is False


def test_portfolio_budget_is_a_real_stop_condition():
    portfolio = build_strategy_portfolio(
        [_entry("BOOLEAN", "AND", "e1"), _entry("BOOLEAN", "OR", "e2")],
        {"strategy_migration": {"recommended_strategies": ["TIME_BASED"]}},
        max_attempts=2,
    )

    assert portfolio["search_exhausted"] is True
    assert portfolio["remaining_strategies"]


def test_business_baselines_do_not_consume_security_strategy_budget():
    history = [
        {
            "experiment_id": "baseline-valid",
            "strategy_family": "BUSINESS_BASELINE",
            "strategy_variant": "VALID",
            "status": "CONFIRMED",
        },
        {
            "experiment_id": "baseline-invalid",
            "strategy_family": "BUSINESS_BASELINE",
            "strategy_variant": "INVALID",
            "status": "CONFIRMED",
        },
        _entry("BOOLEAN", "AND", "boolean-and"),
    ]

    portfolio = build_strategy_portfolio(history, {"vulnerability_type": "SQL_INJECTION"})

    assert [item["experiment_id"] for item in portfolio["tried_strategies"]] == ["boolean-and"]
    assert portfolio["attempts"] == 1
    assert "BOOLEAN_AND" not in portfolio["next_candidates"]


@pytest.fixture
async def state_context(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        challenge = Challenge(
            name="strategy-continuation",
            target_url="http://asset.local",
            allowed_hosts=["asset.local"],
            challenge_type="WEB_TARGET",
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="PLANNING")
        session.add(run)
        await session.flush()
        state = await solver_state_service.initialize(session, run, "WEB_TARGET", [], "feedback", "")
        state.attack_strategy_history_json = [_entry("BOOLEAN", "AND", "e-and")]
        state.last_experiment_json = {
            **state.last_experiment_json,
            "strategy_migration": {"recommended_strategies": ["BOOLEAN_OR"]},
        }
        await session.commit()
        yield session, run
    await engine.dispose()


@pytest.mark.asyncio
async def test_portfolio_is_persisted_in_legacy_solver_state_json(state_context):
    session, run = state_context
    portfolio = await strategy_continuation_service.update(session, run.id)
    await session.commit()
    state = await solver_state_service.load(session, run.id)

    assert portfolio["next_candidates"]
    assert portfolio["next_candidates"][0] == "BOOLEAN_OR"
    assert state.capability_ledger_json["strategy_portfolio"]["next_candidates"][0] == "BOOLEAN_OR"
    assert state.last_experiment_json["strategy_portfolio"]["current_strategy"] == "BOOLEAN_AND"
