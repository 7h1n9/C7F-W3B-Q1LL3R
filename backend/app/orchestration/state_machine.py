from datetime import UTC, datetime
from enum import StrEnum

from app.core.exceptions import DomainError


class RunStatus(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    EVALUATING = "EVALUATING"
    WAITING_USER = "WAITING_USER"
    VERIFYING_FLAG = "VERIFYING_FLAG"
    REPORTING = "REPORTING"
    COMPLETED_SOLVED = "COMPLETED_SOLVED"
    COMPLETED_UNSOLVED = "COMPLETED_UNSOLVED"
    FAILED_ENGINE = "FAILED_ENGINE"
    FAILED_TOOL = "FAILED_TOOL"
    FAILED_RUNNER = "FAILED_RUNNER"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


TERMINAL = {status for status in RunStatus if status.name.startswith(("COMPLETED", "FAILED"))} | {RunStatus.TIMEOUT, RunStatus.CANCELLED, RunStatus.POLICY_BLOCKED}
ALLOWED: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.PREPARING, RunStatus.CANCELLED, RunStatus.POLICY_BLOCKED},
    RunStatus.PREPARING: {RunStatus.ANALYZING, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.ANALYZING: {RunStatus.PLANNING, RunStatus.WAITING_USER, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.PLANNING: {RunStatus.EXECUTING, RunStatus.WAITING_USER, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.EXECUTING: {RunStatus.EVALUATING, RunStatus.FAILED_ENGINE, RunStatus.FAILED_TOOL, RunStatus.FAILED_RUNNER, RunStatus.TIMEOUT, RunStatus.CANCELLED},
    RunStatus.EVALUATING: {RunStatus.PLANNING, RunStatus.VERIFYING_FLAG, RunStatus.REPORTING, RunStatus.WAITING_USER, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.WAITING_USER: {RunStatus.PLANNING, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.VERIFYING_FLAG: {RunStatus.REPORTING, RunStatus.PLANNING, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.REPORTING: {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
}


def transition(run: object, target: RunStatus) -> None:
    current = RunStatus(getattr(run, "status"))
    if target not in ALLOWED.get(current, set()):
        raise DomainError("RUN_INVALID_STATE", "The run cannot be transitioned from its current state.", {"current_state": current, "requested_state": target})
    run.status, run.current_phase = target.value, target.value
    if target == RunStatus.PREPARING and not getattr(run, "started_at"):
        run.started_at = datetime.now(UTC)
    if target in TERMINAL:
        run.finished_at = datetime.now(UTC)
