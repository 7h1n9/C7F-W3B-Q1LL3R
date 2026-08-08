from __future__ import annotations

import json
from typing import Any

import pytest

from app.solver.action import ActionIntent
from app.solver.action_lifecycle import ActionExecutionRecord
from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.events import AUDIT_EVENT_TYPES, SolverAuditEvent, SolverAuditEventType
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import MockWorker, Worker, WorkerManager, WorkerResult


class MemoryRepository:
    def __init__(self, state: BlackboardState) -> None:
        self.state = state

    async def load(self, run_id: str) -> BlackboardState | None:
        return self.state.copy_for_read() if self.state.run_id == run_id else None

    async def save(self, state: BlackboardState) -> BlackboardState:
        self.state = state.copy_for_read()
        return self.state.copy_for_read()

    async def update(
        self,
        run_id: str,
        patch: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BlackboardState:
        current = await self.load(run_id)
        assert current is not None
        if expected_version is not None:
            assert current.version == expected_version
        return await self.save(apply_patch(current, patch))


def make_state(*, control: dict[str, Any] | None = None) -> BlackboardState:
    return BlackboardState(
        run_id="audit-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/search"},
        control=control or {},
    )


class FailingWorker(Worker):
    async def execute(self, action: ActionIntent) -> WorkerResult:
        return WorkerResult(
            success=False,
            action_name=action.action_name,
            output={"error_code": "TARGET_UNAVAILABLE"},
            metadata={"status": "FAILED"},
        )


class ExplodingWorker(Worker):
    async def execute(self, action: ActionIntent) -> WorkerResult:
        raise RuntimeError("worker interrupted")


def make_loop(
    state: BlackboardState,
    worker: Worker | None = None,
) -> tuple[SolverLoop, MemoryRepository, Worker]:
    repository = MemoryRepository(state)
    active_worker = worker or MockWorker()
    return (
        SolverLoop(
            repository,
            state_machine=TaskStateMachine(),
            planner=DeterministicPlanner(),
            policy=ActionPolicyValidator(),
            worker_manager=WorkerManager(workers={"mock": active_worker}),
        ),
        repository,
        active_worker,
    )


def audit_entries(state: BlackboardState) -> list[dict[str, Any]]:
    return [item["audit"] for item in state.history if "audit" in item]


@pytest.mark.asyncio
async def test_action_lifecycle_audit_fields_are_complete_and_allowlisted() -> None:
    loop, repository, _ = make_loop(make_state())

    await loop.step("audit-test")

    entries = audit_entries(repository.state)
    event_types = {item["event_type"] for item in entries}
    assert {
        SolverAuditEventType.ACTION_PLANNED.value,
        SolverAuditEventType.ACTION_AUTHORIZED.value,
        SolverAuditEventType.ACTION_STARTED.value,
        SolverAuditEventType.ACTION_COMPLETED.value,
    } <= event_types
    required_fields = {
        "event_type",
        "run_id",
        "step",
        "phase",
        "action_name",
        "action_id",
        "fingerprint",
        "status",
        "reason_code",
        "evidence_refs",
        "blackboard_version",
        "source",
        "timestamp",
    }
    for entry in entries:
        assert set(entry) == required_fields
        assert entry["run_id"] == "audit-test"
        assert entry["event_type"] in AUDIT_EVENT_TYPES
        assert entry["action_id"]
        assert entry["fingerprint"]
        assert entry["blackboard_version"] > 0
        assert "payload" not in entry


@pytest.mark.asyncio
async def test_recovery_emits_a_recovered_audit_event_without_worker_execution() -> None:
    action = ActionIntent("http_request", "recover", {"url": "http://target.test/search"})
    started = ActionExecutionRecord.pending(action).started()
    loop, repository, worker = make_loop(
        make_state(control={"active_action": started.to_dict()})
    )

    step = await loop.step("audit-test")

    recovered = audit_entries(repository.state)[-1]
    assert step.event.event_type == "ACTION_INTERRUPTED"
    assert recovered["event_type"] == SolverAuditEventType.ACTION_RECOVERED.value
    assert recovered["status"] == "INTERRUPTED"
    assert recovered["reason_code"] == "IN_FLIGHT_ACTION_DETECTED"
    assert worker.calls == []


@pytest.mark.asyncio
async def test_failed_and_interrupted_actions_emit_typed_audit_events() -> None:
    failed_loop, failed_repository, _ = make_loop(make_state(), FailingWorker())
    await failed_loop.step("audit-test")

    failed = audit_entries(failed_repository.state)[-1]
    assert failed["event_type"] == SolverAuditEventType.ACTION_FAILED.value
    assert failed["status"] == "FAILED"
    assert failed["reason_code"] == "TARGET_UNAVAILABLE"

    interrupted_loop, interrupted_repository, _ = make_loop(make_state(), ExplodingWorker())
    with pytest.raises(RuntimeError, match="worker interrupted"):
        await interrupted_loop.step("audit-test")

    interrupted = audit_entries(interrupted_repository.state)[-1]
    assert interrupted["event_type"] == SolverAuditEventType.ACTION_INTERRUPTED.value
    assert interrupted["status"] == "INTERRUPTED"


def test_completion_event_is_serializable_without_payload() -> None:
    event = SolverAuditEvent(
        event_type=SolverAuditEventType.COMPLETION_EVALUATED.value,
        run_id="audit-test",
        step=3,
        phase="REPORTING",
        status="UNSOLVED",
        reason_code="FINDING_REQUIRED",
        evidence_refs=("evidence://one",),
        blackboard_version=9,
    )

    serialized = event.to_dict()

    assert json.loads(json.dumps(serialized)) == serialized
    assert serialized["evidence_refs"] == ["evidence://one"]
    assert "payload" not in serialized


def test_audit_event_rejects_arbitrary_payload_and_sensitive_values() -> None:
    with pytest.raises(TypeError):
        SolverAuditEvent(
            event_type=SolverAuditEventType.ACTION_COMPLETED.value,
            run_id="audit-test",
            payload={"raw_result": "response"},  # type: ignore[call-arg]
        )

    with pytest.raises(ValueError):
        SolverAuditEvent(
            event_type=SolverAuditEventType.ACTION_COMPLETED.value,
            run_id="audit-test",
            reason_code="secret-token",
        )


def test_audit_event_persists_only_evidence_references() -> None:
    event = SolverAuditEvent(
        event_type=SolverAuditEventType.ACTION_COMPLETED.value,
        run_id="audit-test",
        action_name="http_request",
        action_id="action-1",
        fingerprint="fingerprint-1",
        evidence_refs=("evidence-1", "evidence-2"),
    )

    serialized = event.to_dict()

    assert serialized["evidence_refs"] == ["evidence-1", "evidence-2"]
    assert "response body" not in json.dumps(serialized)
    assert "raw_result" not in serialized
