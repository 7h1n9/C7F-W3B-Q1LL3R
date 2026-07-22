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
    PAUSED_RATE_LIMIT = "PAUSED_RATE_LIMIT"
    RETRYING = "RETRYING"
    PAUSED_CHECKPOINT = "PAUSED_CHECKPOINT"
    PAUSED_RECOVERY = "PAUSED_RECOVERY"
    PAUSED_DEPLOYMENT = "PAUSED_DEPLOYMENT"
    WAITING_CONFIGURATION = "WAITING_CONFIGURATION"


TERMINAL = {status for status in RunStatus if status.name.startswith(("COMPLETED", "FAILED"))} | {
    RunStatus.TIMEOUT,
    RunStatus.CANCELLED,
    RunStatus.POLICY_BLOCKED,
}
RESTARTABLE = {
    RunStatus.WAITING_USER,
    RunStatus.FAILED_ENGINE,
    RunStatus.FAILED_TOOL,
    RunStatus.FAILED_RUNNER,
    RunStatus.TIMEOUT,
    RunStatus.COMPLETED_UNSOLVED,
    RunStatus.CANCELLED,
    RunStatus.PAUSED_RATE_LIMIT,
    RunStatus.PAUSED_CHECKPOINT,
    RunStatus.PAUSED_RECOVERY,
    RunStatus.PAUSED_DEPLOYMENT,
    RunStatus.WAITING_CONFIGURATION,
}
TIMEOUT_SOURCES = {
    RunStatus.CREATED,
    RunStatus.PREPARING,
    RunStatus.ANALYZING,
    RunStatus.PLANNING,
    RunStatus.EXECUTING,
    RunStatus.EVALUATING,
    RunStatus.WAITING_USER,
    RunStatus.VERIFYING_FLAG,
    RunStatus.REPORTING,
    RunStatus.RETRYING,
}
ALLOWED: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {
        RunStatus.PREPARING,
        RunStatus.FAILED_ENGINE,
        RunStatus.FAILED_RUNNER,
        RunStatus.CANCELLED,
        RunStatus.POLICY_BLOCKED,
        RunStatus.WAITING_CONFIGURATION,
    },
    RunStatus.PREPARING: {
        RunStatus.ANALYZING,
        RunStatus.FAILED_ENGINE,
        RunStatus.FAILED_RUNNER,
        RunStatus.CANCELLED,
    },
    RunStatus.ANALYZING: {
        RunStatus.PLANNING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.PLANNING: {
        RunStatus.EXECUTING,
        RunStatus.VERIFYING_FLAG,
        RunStatus.REPORTING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.EXECUTING: {
        RunStatus.EVALUATING,
        # A controlled stop (budget/no-progress ceiling) may finish directly
        # after a rejected or failed tool action, before EVALUATING is entered.
        RunStatus.REPORTING,
        RunStatus.FAILED_ENGINE,
        RunStatus.FAILED_TOOL,
        RunStatus.FAILED_RUNNER,
        RunStatus.TIMEOUT,
        RunStatus.CANCELLED,
    },
    RunStatus.EVALUATING: {
        RunStatus.PLANNING,
        RunStatus.VERIFYING_FLAG,
        RunStatus.REPORTING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_USER: {RunStatus.PLANNING, RunStatus.WAITING_CONFIGURATION, RunStatus.FAILED_ENGINE, RunStatus.CANCELLED},
    RunStatus.VERIFYING_FLAG: {
        RunStatus.REPORTING,
        RunStatus.PLANNING,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.REPORTING: {
        RunStatus.COMPLETED_SOLVED,
        RunStatus.COMPLETED_UNSOLVED,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.PAUSED_RATE_LIMIT: {RunStatus.PLANNING, RunStatus.WAITING_USER, RunStatus.CANCELLED},
    RunStatus.PAUSED_CHECKPOINT: {
        RunStatus.PLANNING,
        RunStatus.PAUSED_RECOVERY,
        RunStatus.CANCELLED,
    },
    RunStatus.PAUSED_RECOVERY: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.PAUSED_DEPLOYMENT: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.WAITING_CONFIGURATION: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.RETRYING: {RunStatus.PLANNING, RunStatus.WAITING_CONFIGURATION, RunStatus.CANCELLED},
}

for status in TIMEOUT_SOURCES:
    if status not in TERMINAL:
        ALLOWED.setdefault(status, set()).add(RunStatus.TIMEOUT)
    ALLOWED.setdefault(status, set()).add(RunStatus.PAUSED_RATE_LIMIT)
    ALLOWED.setdefault(status, set()).update(
        {
            RunStatus.RETRYING,
            RunStatus.PAUSED_CHECKPOINT,
            RunStatus.PAUSED_RECOVERY,
            RunStatus.PAUSED_DEPLOYMENT,
            RunStatus.WAITING_CONFIGURATION,
        }
    )

# A planned service restart may encounter any non-terminal phase, including
# checkpoint/rate-limit/configuration pauses that are not timeout sources.
# Reconcile those runs into the explicit deployment-pause state instead of
# aborting application startup on an invalid transition.
for status in RunStatus:
    if status not in TERMINAL:
        ALLOWED.setdefault(status, set()).add(RunStatus.PAUSED_DEPLOYMENT)


def transition(run: object, target: RunStatus) -> None:
    current = RunStatus(getattr(run, "status"))
    if current == RunStatus.COMPLETED_SOLVED and target != current:
        raise DomainError(
            "RUN_TERMINAL_IMMUTABLE",
            "A verified solved run cannot be resumed or overwritten.",
            {"current_state": current, "requested_state": target},
        )
    if target not in ALLOWED.get(current, set()):
        raise DomainError(
            "RUN_INVALID_STATE",
            "The run cannot be transitioned from its current state.",
            {"current_state": current, "requested_state": target},
        )
    run.status, run.current_phase = target.value, target.value
    if target == RunStatus.PREPARING and not getattr(run, "started_at"):
        run.started_at = datetime.now(UTC)
    if target in TERMINAL:
        run.finished_at = datetime.now(UTC)


def restart(run: object) -> RunStatus:
    """Re-arm a run without deleting its durable state, events, or evidence."""
    current = RunStatus(getattr(run, "status"))
    if current not in RESTARTABLE:
        raise DomainError(
            "RUN_NOT_RESTARTABLE",
            "Only waiting, failed, timed-out, cancelled, or unsolved runs can restart.",
            {"current_state": current},
        )
    run.status = RunStatus.WAITING_USER.value
    run.current_phase = RunStatus.WAITING_USER.value
    run.finished_at = None
    # Restart creates a fresh Attempt but must never erase durable Run totals.
    # Legacy counters mirror the current Attempt for compatibility.
    run.agent_step_count = 0
    run.tool_call_count = 0
    run.attempt_agent_steps = 0
    run.attempt_logical_tool_calls = 0
    run.checkpoint_segment_steps = 0
    run.infrastructure_retry_count = 0
    return current
