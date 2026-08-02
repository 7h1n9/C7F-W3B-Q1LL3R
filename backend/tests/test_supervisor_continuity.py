from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import RunUserInput, SolveRun
from app.models.solver_state import SolverState
from app.schemas.multi_agent import FailureClassification
from app.services.failure_classification import normalize_failure_classification
from app.services.stage_decider import stage_decider
from app.services.tool_failure_policy import tool_failure_fingerprint
from app.services.user_input_consumer import consume_user_inputs


def test_failure_classification_normalizes_pydantic_object() -> None:
    value = FailureClassification(
        fingerprint="fp",
        classification="MYSQL_METADATA_EMPTY_RESULT",
    )
    assert normalize_failure_classification(value)["classification"] == "MYSQL_METADATA_EMPTY_RESULT"


def test_failure_classification_normalizes_legacy_object() -> None:
    class LegacyClassification:
        def __init__(self):
            self.classification = "TOOL_FAILURE"

    assert normalize_failure_classification(LegacyClassification())["classification"] == "TOOL_FAILURE"


def test_stage_decider_ignores_stale_phase_and_requires_metadata() -> None:
    decision = stage_decider.decide(
        asset_warranty_mysql=True,
        verified_fact_keys={
            "asset_warranty.valid_baseline",
            "asset_warranty.invalid_baseline",
            "asset_warranty.mysql_boolean_oracle",
            "asset_warranty.mysql_dbms",
            "asset_warranty.oracle_calibration_matrix",
        },
    )
    assert decision.stage == "MYSQL_METADATA_DISCOVERY"
    assert decision.details["stage"] == "version"


def test_tool_failure_fingerprint_is_stable_and_stage_sensitive() -> None:
    first = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "version", "VERSION()", "args-digest")
    same = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "version", "VERSION()", "args-digest")
    different_stage = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "tables", "VERSION()", "args-digest")
    assert first == same
    assert first != different_stage


@pytest.mark.asyncio
async def test_consume_user_inputs_marks_all_queued_rows_and_updates_hints(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'inputs.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="input", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="WAITING_USER")
        session.add(run)
        await session.flush()
        state = SolverState(run_id=run.id)
        session.add(state)
        session.add_all([
            RunUserInput(run_id=run.id, revision=2, content="try alternative strategy"),
            RunUserInput(run_id=run.id, revision=1, content="continue metadata"),
        ])
        await session.commit()
        consumed = await consume_user_inputs(session, run)
        rows = list((await session.scalars(select(RunUserInput).where(RunUserInput.run_id == run.id).order_by(RunUserInput.revision))).all())
        refreshed_state = await session.get(SolverState, state.id)
        assert [item["revision"] for item in consumed["user_inputs"]] == [1, 2]
        assert all(item.status == "CONSUMED" and item.consumed_at is not None for item in rows)
        assert run.hints_json["user_inputs"][0]["content"] == "continue metadata"
        assert refreshed_state.last_decision_card_json["user_inputs"]
    await engine.dispose()
