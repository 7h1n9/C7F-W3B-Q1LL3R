import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.solver_state import SolverState
from app.models.run import SolveRun
from app.services.boolean_oracle_diagnosis import diagnose_boolean_oracle, boolean_oracle_diagnosis_service


def _payload(**values):
    return {
        "stable_true": True,
        "stable_false": True,
        "response_differential": True,
        "boolean_oracle_confirmed": True,
        "request_contract": {"method": "POST", "url": "http://target.test/check"},
        "test_field": "asset_no",
        "baseline_value": "PC-2026-013",
        "oracle": {"json_field": "matched"},
        "control_fields": {"department": "OPS"},
        **values,
    }


@pytest.mark.parametrize(("values", "classification", "next_action"), [
    ({}, "ORACLE_CONFIRMED", "enter_calibration"),
    ({"stable_true": False, "stable_false": True, "response_differential": False, "boolean_oracle_confirmed": False}, "TRUE_SIDE_FAILED", "retry_true_condition"),
    ({"stable_true": True, "stable_false": False, "response_differential": False, "boolean_oracle_confirmed": False}, "FALSE_SIDE_FAILED", "validate_negative_control"),
    ({"stable_true": True, "stable_false": True, "response_differential": False, "boolean_oracle_confirmed": False}, "NO_DIFFERENCE", "change_signal_strategy"),
    ({"stable_true": False, "stable_false": False, "response_differential": False, "boolean_oracle_confirmed": False}, "NO_SIGNAL", "change_payload_family"),
])
def test_boolean_oracle_diagnosis(values, classification, next_action):
    result = diagnose_boolean_oracle(_payload(**values))
    assert result["classification"] == classification
    assert result["next_action"] == next_action
    assert result["recommended_strategy"]


@pytest.mark.asyncio
async def test_failed_payload_is_recorded_for_planner_memory(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        state = SolverState(run_id="run-1")
        session.add(state)
        run = SolveRun(id="run-1", challenge_id="challenge-1", workspace_path=str(tmp_path))
        session.add(run)
        await session.flush()
        diagnosis = await boolean_oracle_diagnosis_service.record(
            session, "run-1", _payload(stable_true=False, response_differential=False, boolean_oracle_confirmed=False)
        )
        assert diagnosis["classification"] == "TRUE_SIDE_FAILED"
        assert state.capability_ledger_json["boolean_failure_history"][0]["next_action"] == "retry_true_condition"
        assert state.security_context_json["boolean_diagnosis"]["classification"] == "TRUE_SIDE_FAILED"
        assert run.recovery_checkpoint_json["do_not_repeat_boolean_payload_fingerprint"] == diagnosis["payload_fingerprint"]
    await engine.dispose()
