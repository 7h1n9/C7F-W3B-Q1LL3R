from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.run import RunEvent
from app.solver.muteki.adapter.cost_bridge import (
    load_muteki_usage,
    record_official_worker_usage,
    usage_breakdown_from_events,
    usage_from_events,
    usage_from_result,
)


def test_usage_from_result_uses_safe_metadata_only() -> None:
    result = SimpleNamespace(
        engine="codex",
        output="secret target response must not be inspected",
        metadata={"input_tokens": 12, "output_tokens": 8, "cost_usd": 0.1234},
    )

    usage = usage_from_result(result)

    assert usage.input_tokens == 12
    assert usage.output_tokens == 8
    assert usage.tokens == 20
    assert usage.cost_usd == 0.1234
    assert usage.calls == 1


def test_usage_from_late_result_is_ignored_after_recovery_flush() -> None:
    result = SimpleNamespace(
        engine="step-3.7-flash",
        metadata={"_usage_already_recorded": True, "input_tokens": 10, "output_tokens": 5, "calls": 1},
    )
    usage = usage_from_result(result)
    assert usage.tokens == 0
    assert usage.calls == 0


def test_usage_from_result_prices_known_engine_when_driver_has_no_cost() -> None:
    usage = usage_from_result(
        SimpleNamespace(engine="codex", metadata={"input_tokens": 1_000, "output_tokens": 2_000})
    )

    assert usage.tokens == 3_000
    assert usage.cost_usd > 0


def test_usage_from_events_sums_bridge_deltas() -> None:
    events = [
        SimpleNamespace(
            event_type="cost.update",
            payload_json={
                "delta_usd": 0.01,
                "delta_tokens": 3,
                "delta_input_tokens": 1,
                "delta_output_tokens": 2,
                "delta_calls": 1,
                "tokens": 3,
            },
        ),
        SimpleNamespace(
            event_type="cost.update",
            payload_json={
                "delta_usd": 0.02,
                "delta_tokens": 4,
                "delta_input_tokens": 2,
                "delta_output_tokens": 2,
                "delta_calls": 1,
                "tokens": 7,
            },
        ),
    ]

    usage = usage_from_events(events)

    assert usage.cost_usd == pytest.approx(0.03)
    assert usage.input_tokens == 3
    assert usage.output_tokens == 4
    assert usage.tokens == 7
    assert usage.calls == 2


def test_usage_breakdown_groups_models_and_roles() -> None:
    events = [
        SimpleNamespace(
            event_type="cost.update",
            payload_json={
                "model": "codex",
                "role": "worker",
                "source": "muteki.official_worker",
                "delta_input_tokens": 10,
                "delta_output_tokens": 4,
                "delta_tokens": 14,
                "delta_usd": 0.2,
                "delta_calls": 1,
            },
        ),
        SimpleNamespace(
            event_type="cost.update",
            payload_json={
                "model": "deepseek-chat",
                "role": "coordinator_reason",
                "source": "muteki.coordinator_reason",
                "delta_input_tokens": 7,
                "delta_output_tokens": 3,
                "delta_tokens": 10,
                "delta_usd": 0.03,
                "delta_calls": 1,
            },
        ),
    ]

    rows = usage_breakdown_from_events(events)

    assert len(rows) == 2
    assert rows[0]["model"] == "codex"
    assert rows[0]["total_tokens"] == 14
    assert rows[0]["cost_usd"] == pytest.approx(0.2)
    assert rows[1]["role"] == "coordinator_reason"
    assert rows[1]["total_tokens"] == 10


@pytest.mark.asyncio
async def test_record_usage_persists_cumulative_run_event_and_report_summary() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    first = SimpleNamespace(
        engine="codex",
        metadata={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25},
    )
    second = SimpleNamespace(
        engine="codex",
        metadata={"input_tokens": 3, "output_tokens": 2, "cost_usd": 0.05},
    )

    await record_official_worker_usage(
        factory,
        run_id="run-cost",
        worker_id="worker-1",
        intent_id="intent-1",
        result=first,
    )
    await record_official_worker_usage(
        factory,
        run_id="run-cost",
        worker_id="worker-2",
        intent_id="intent-2",
        result=second,
    )

    async with factory() as session:
        events = list(
            (
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == "run-cost", RunEvent.event_type == "cost.update")
                    .order_by(RunEvent.sequence)
                )
            ).all()
        )
        usage = await load_muteki_usage(session, "run-cost")

    assert len(events) == 2
    assert events[-1].payload_json["tokens"] == 20
    assert events[-1].payload_json["input_tokens"] == 13
    assert events[-1].payload_json["output_tokens"] == 7
    assert events[-1].payload_json["cost_usd"] == pytest.approx(0.3)
    assert usage.tokens == 20
    assert usage.calls == 2
    await engine.dispose()
