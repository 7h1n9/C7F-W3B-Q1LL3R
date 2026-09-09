from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.services.solver_scoring import (
    EFFICIENCY_CAP,
    SOLVED_WEIGHT,
    TIME_WEIGHT,
    TOKEN_WEIGHT,
    build_prediction_inputs,
    challenge_fingerprint,
    compute_score_components,
    parse_prediction_document,
    _resolve_prediction_model_config,
    _save_prediction,
    _select_prediction_model_config,
)


def _challenge(**overrides):
    values = {
        "id": "challenge-1",
        "name": "SQL 注入靶场",
        "description": "一个简单的 SQL 注入题目。",
        "challenge_type": "WEB_TARGET",
        "target_url": "http://127.0.0.1:8001",
        "metadata_json": {"adapter": "flask", "dbms": "sqlite"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _attachment(name="app.py", kind="SOURCE", sha256="abc", size=1024, is_primary=False):
    return SimpleNamespace(
        original_name=name,
        kind=kind,
        size=size,
        sha256=sha256,
        is_primary=is_primary,
    )


def _model_config(
    config_id: str,
    *,
    roles: list[str] | None = None,
    enabled: bool = True,
    provider_type: str = "openai_compatible",
):
    return SimpleNamespace(
        id=config_id,
        name=config_id,
        model_name=config_id,
        enabled=enabled,
        provider_type=provider_type,
        capabilities_json={"roles": roles or ["coordinator_reason"]},
        encrypted_api_key="encrypted",
        base_url="https://model.example/v1",
    )


def test_challenge_fingerprint_changes_with_content() -> None:
    before = challenge_fingerprint(_challenge(), [_attachment()])
    after = challenge_fingerprint(_challenge(description="题目描述已更新。"), [_attachment()])
    assert before != after


def test_challenge_fingerprint_is_stable() -> None:
    assert challenge_fingerprint(_challenge(), [_attachment()]) == challenge_fingerprint(
        _challenge(), [_attachment()]
    )


def test_build_prediction_inputs_bounds_metadata() -> None:
    challenge = _challenge(metadata_json={"dbms": "mysql", "secret": "keep-out"})
    inputs = build_prediction_inputs(challenge, [_attachment()])
    assert inputs["metadata"] == {"dbms": "mysql"}
    assert "secret" not in inputs["metadata"]
    assert inputs["attachments"][0]["name"] == "app.py"


def test_parse_prediction_document_normalizes() -> None:
    raw = (
        '{"predicted_solve_seconds": 900, "predicted_tokens": 120000, '
        '"predicted_tool_calls": 60, "difficulty": "hard", '
        '"confidence": 0.8, "rationale_zh": "数据库注入需要枚举"}'
    )
    parsed = parse_prediction_document(raw)
    assert parsed["predicted_solve_seconds"] == 900
    assert parsed["predicted_tokens"] == 120000
    assert parsed["predicted_tool_calls"] == 60
    assert parsed["difficulty"] == "hard"
    assert parsed["confidence"] == 0.8


@pytest.mark.parametrize(
    "field,value",
    [
        ("predicted_solve_seconds", 10),
        ("predicted_solve_seconds", 100000),
        ("predicted_tokens", 1),
        ("predicted_tool_calls", 0),
    ],
)
def test_parse_prediction_document_rejects_out_of_range(field: str, value: int) -> None:
    raw = {
        "predicted_solve_seconds": 900,
        "predicted_tokens": 120000,
        "predicted_tool_calls": 60,
        field: value,
    }
    with pytest.raises(ValueError):
        parse_prediction_document(raw)


def test_compute_score_meets_prediction() -> None:
    result = compute_score_components(
        predicted_seconds=900,
        predicted_tokens=120000,
        actual_seconds=900,
        actual_tokens=120000,
        solved=True,
    )
    assert result["time_points"] == TIME_WEIGHT
    assert result["token_points"] == TOKEN_WEIGHT
    assert result["solved_points"] == SOLVED_WEIGHT
    assert result["total_score"] == 100.0


def test_compute_score_bonus_capped_at_two_times() -> None:
    result = compute_score_components(
        predicted_seconds=900,
        predicted_tokens=120000,
        actual_seconds=450,
        actual_tokens=60000,
        solved=True,
    )
    assert result["time_points"] == TIME_WEIGHT * EFFICIENCY_CAP
    assert result["token_points"] == TOKEN_WEIGHT * EFFICIENCY_CAP
    assert result["total_score"] == 150.0


def test_compute_score_unsolved_keeps_efficiency_points() -> None:
    result = compute_score_components(
        predicted_seconds=900,
        predicted_tokens=120000,
        actual_seconds=450,
        actual_tokens=60000,
        solved=False,
    )
    assert result["solved_points"] == 0
    assert result["total_score"] == 100.0


def test_compute_score_missing_actuals_only_solved_points() -> None:
    result = compute_score_components(
        predicted_seconds=900,
        predicted_tokens=120000,
        actual_seconds=None,
        actual_tokens=None,
        solved=True,
    )
    assert result["total_score"] == SOLVED_WEIGHT
    assert result["time_ratio"] is None
    assert result["token_ratio"] is None


def test_compute_score_zero_tokens_gets_cap_bonus() -> None:
    result = compute_score_components(
        predicted_seconds=900,
        predicted_tokens=120000,
        actual_seconds=900,
        actual_tokens=0,
        solved=False,
    )
    assert result["token_ratio"] == 0.0
    assert result["token_points"] == TOKEN_WEIGHT * EFFICIENCY_CAP


def test_select_prediction_model_prefers_explicit_reason() -> None:
    fallback = _model_config("fallback")
    reason = _model_config("reason")

    selected = _select_prediction_model_config(
        [fallback, reason],
        reason_model_config_id="reason",
    )

    assert selected is reason


def test_select_prediction_model_rejects_invalid_explicit_reason() -> None:
    fallback = _model_config("fallback")
    invalid = _model_config("invalid", roles=["worker"])

    with pytest.raises(ValueError, match="PREDICTION_MODEL_UNAVAILABLE"):
        _select_prediction_model_config(
            [fallback, invalid],
            reason_model_config_id="invalid",
        )


def test_select_prediction_model_falls_back_to_first_valid_reason() -> None:
    worker_only = _model_config("worker", roles=["worker"])
    disabled = _model_config("disabled", enabled=False)
    valid = _model_config("valid")

    selected = _select_prediction_model_config([worker_only, disabled, valid])

    assert selected is valid


@pytest.mark.asyncio
async def test_resolve_prediction_model_uses_latest_run_reason() -> None:
    from app.models import Base, ModelConfig, SolveRun
    from app.solver.muteki.runtime.configuration import write_runtime_selection

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        older = ModelConfig(
            name="older-reason",
            provider_type="openai_compatible",
            base_url="https://older.example/v1",
            model_name="older-reason",
            encrypted_api_key="encrypted",
            enabled=True,
            capabilities_json={"roles": ["coordinator_reason"]},
        )
        newer = ModelConfig(
            name="newer-reason",
            provider_type="openai_compatible",
            base_url="https://newer.example/v1",
            model_name="newer-reason",
            encrypted_api_key="encrypted",
            enabled=True,
            capabilities_json={"roles": ["coordinator_reason"]},
        )
        session.add_all([older, newer])
        await session.flush()
        session.add_all(
            [
                SolveRun(
                    challenge_id="challenge-1",
                    workspace_path="older",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    hints_json=write_runtime_selection(
                        {},
                        reason_model_config_id=older.id,
                        worker_engines=(),
                    ),
                ),
                SolveRun(
                    challenge_id="challenge-1",
                    workspace_path="newer",
                    created_at=datetime(2026, 1, 2, tzinfo=UTC),
                    hints_json=write_runtime_selection(
                        {},
                        reason_model_config_id=newer.id,
                        worker_engines=(),
                    ),
                ),
            ]
        )
        await session.commit()

        selected = await _resolve_prediction_model_config(
            session,
            challenge_id="challenge-1",
            reason_model_config_id=None,
        )

    await engine.dispose()
    assert selected.id == newer.id


@pytest.mark.asyncio
async def test_save_prediction_persists_actual_model_name() -> None:
    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        item = await _save_prediction(
            session,
            challenge_id="challenge-1",
            status="COMPLETED",
            fingerprint="fingerprint",
            version=1,
            data={
                "predicted_solve_seconds": 900,
                "predicted_tokens": 120000,
                "predicted_tool_calls": 60,
                "difficulty": "medium",
                "confidence": 0.8,
                "rationale_zh": "test",
            },
            usage={"total_tokens": 10},
            model="newer-reason",
        )

    await engine.dispose()
    assert item.model == "newer-reason"
