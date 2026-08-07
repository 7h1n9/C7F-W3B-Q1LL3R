from __future__ import annotations

from typing import Any

import pytest

from app.solver.blackboard import (
    BlackboardState,
    BlackboardVersionConflict,
    MySQLBlackboardStore,
    deserialize_state,
    serialize_state,
)
from app.solver.blackboard.mysql_store import BlackboardRecord


class FakeSession:
    """Session-shaped test double; it never connects to a real database."""

    def __init__(self) -> None:
        self.records: dict[str, BlackboardRecord] = {}

    async def get(self, model: type[BlackboardRecord], run_id: str) -> BlackboardRecord | None:
        assert model is BlackboardRecord
        return self.records.get(run_id)

    def add(self, record: BlackboardRecord) -> None:
        self.records[record.run_id] = record

    async def flush(self) -> None:
        return None


@pytest.fixture
def state() -> BlackboardState:
    return BlackboardState(
        run_id="blackboard-test",
        phase="BASELINE",
        goal="establish a baseline",
        knowledge={"facts": [{"key": "entrypoint", "value": "/search"}]},
        control={"allowed_actions": ["http_request"]},
        history=[{"type": "initialized"}],
        evidence_refs=["evidence-0"],
    )


async def test_mysql_store_saves_and_loads_state(state: BlackboardState) -> None:
    session = FakeSession()
    store = MySQLBlackboardStore(session)

    saved = await store.save(state)
    loaded = await store.load(state.run_id)

    assert loaded == saved
    assert loaded is not None
    assert loaded.goal == "establish a baseline"
    assert loaded.knowledge == state.knowledge
    assert loaded.control == state.control
    assert loaded.evidence_refs == ["evidence-0"]


async def test_mysql_store_updates_phase_and_restores_from_new_adapter(state: BlackboardState) -> None:
    session = FakeSession()
    first_store = MySQLBlackboardStore(session)
    await first_store.save(state)

    updated = await first_store.update(
        state.run_id,
        {
            "phase": "VALIDATION",
            "history_append": [{"type": "phase.changed", "phase": "VALIDATION"}],
        },
        expected_version=0,
    )

    restored = await MySQLBlackboardStore(session).load(state.run_id)

    assert updated.version == 1
    assert restored == updated
    assert restored is not None
    assert restored.phase == "VALIDATION"
    assert restored.history[-1]["type"] == "phase.changed"


async def test_update_rejects_stale_version(state: BlackboardState) -> None:
    session = FakeSession()
    store = MySQLBlackboardStore(session)
    await store.save(state)
    await store.update(state.run_id, {"phase": "VALIDATION"}, expected_version=0)

    with pytest.raises(BlackboardVersionConflict):
        await store.update(state.run_id, {"phase": "EXPLOITATION"}, expected_version=0)


def test_serializer_round_trips_and_migrates_phase_1_1_shape(state: BlackboardState) -> None:
    restored = deserialize_state(serialize_state(state))
    legacy_payload: dict[str, Any] = {
        "run_id": "legacy",
        "version": 2,
        "phase": "VALIDATION",
        "facts": [{"key": "baseline", "value": True}],
        "hypotheses": [{"key": "sqli", "confidence": 0.8}],
        "allowed_actions": ["sql_boolean_compare"],
        "evidence_refs": ["evidence-1"],
        "history": [{"type": "legacy"}],
    }
    migrated = deserialize_state(legacy_payload)

    assert restored == state
    assert migrated.schema_version == 1
    assert migrated.facts == legacy_payload["facts"]
    assert migrated.hypotheses == legacy_payload["hypotheses"]
    assert migrated.allowed_actions == legacy_payload["allowed_actions"]
