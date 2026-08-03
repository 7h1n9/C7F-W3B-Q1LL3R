from datetime import UTC, datetime
from enum import StrEnum

from app.core.exceptions import DomainError


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
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
    PAUSED_BUDGET = "PAUSED_BUDGET"
    WAITING_CONFIGURATION = "WAITING_CONFIGURATION"
    INFRASTRUCTURE_VALIDATION = "INFRASTRUCTURE_VALIDATION"


TERMINAL = {status for status in RunStatus if status.name.startswith(("COMPLETED", "FAILED"))} | {
    RunStatus.TIMEOUT,
    RunStatus.CANCELLED,
    RunStatus.POLICY_BLOCKED,
}
SOLVER_PHASES = {
    "INTAKE", "BASELINE", "RECON", "MAPPING", "HYPOTHESIS", "TESTING", "CHAINING", "ENUMERATION",
    "FLAG_SEARCH", "FLAG_VERIFICATION", "REPORTING",
    # Multi-agent solver phases are persisted on SolveRun as solver phases,
    # not lifecycle statuses.  Keep them from being reset to INTAKE when the
    # lifecycle moves through EXECUTING/EVALUATING/PLANNING.
    "BUSINESS_BASELINE", "BOOLEAN_ORACLE", "ORACLE_CALIBRATION",
    "MYSQL_METADATA_DISCOVERY", "BOUNDED_EXTRACTION",
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
    RunStatus.PAUSED_BUDGET,
    RunStatus.WAITING_CONFIGURATION,
}
TIMEOUT_SOURCES = {
    RunStatus.CREATED,
    RunStatus.RUNNING,
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
        RunStatus.RUNNING,
        RunStatus.PREPARING,
        RunStatus.FAILED_ENGINE,
        RunStatus.FAILED_RUNNER,
        RunStatus.CANCELLED,
        RunStatus.POLICY_BLOCKED,
        RunStatus.WAITING_CONFIGURATION,
        RunStatus.INFRASTRUCTURE_VALIDATION,
    },
    RunStatus.PREPARING: {
        RunStatus.RUNNING,
        RunStatus.ANALYZING,
        RunStatus.FAILED_ENGINE,
        RunStatus.FAILED_RUNNER,
        RunStatus.CANCELLED,
    },
    RunStatus.ANALYZING: {
        RunStatus.RUNNING,
        RunStatus.PLANNING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.PLANNING: {
        RunStatus.RUNNING,
        RunStatus.EXECUTING,
        RunStatus.VERIFYING_FLAG,
        RunStatus.REPORTING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED_ENGINE,
        RunStatus.CANCELLED,
    },
    RunStatus.EXECUTING: {
        RunStatus.RUNNING,
        RunStatus.EVALUATING,
        # A controlled stop (budget/no-progress ceiling) may finish directly
        # after a rejected or failed tool action, before EVALUATING is entered.
        RunStatus.REPORTING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED_ENGINE,
        RunStatus.FAILED_TOOL,
        RunStatus.FAILED_RUNNER,
        RunStatus.TIMEOUT,
        RunStatus.CANCELLED,
    },
    RunStatus.EVALUATING: {
        RunStatus.RUNNING,
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
    RunStatus.PAUSED_BUDGET: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.WAITING_CONFIGURATION: {RunStatus.PLANNING, RunStatus.CANCELLED, RunStatus.INFRASTRUCTURE_VALIDATION},
    RunStatus.INFRASTRUCTURE_VALIDATION: {RunStatus.PLANNING, RunStatus.WAITING_CONFIGURATION, RunStatus.CANCELLED},
    RunStatus.RETRYING: {RunStatus.PLANNING, RunStatus.WAITING_CONFIGURATION, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.ANALYZING,
        RunStatus.PLANNING,
        RunStatus.EXECUTING,
        RunStatus.EVALUATING,
        RunStatus.VERIFYING_FLAG,
        RunStatus.REPORTING,
        RunStatus.CANCELLED,
    },
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
    run.status = target.value
    # Lifecycle pauses/configuration states must not overwrite the solver's
    # phase.  Legacy runs may still carry a lifecycle value as phase; retain
    # the old mirroring behavior only until a real solver phase exists.
    if target == RunStatus.COMPLETED_SOLVED:
        # Solved is a lifecycle status.  The last solver phase remains
        # FLAG_VERIFICATION/REPORTING and is never replaced by a status value.
        if str(getattr(run, "current_phase", "")) not in SOLVER_PHASES:
            run.current_phase = "REPORTING"
    elif str(getattr(run, "current_phase", "")) not in SOLVER_PHASES:
        run.current_phase = "INTAKE"
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
    if str(getattr(run, "current_phase", "")) not in SOLVER_PHASES:
        run.current_phase = "INTAKE"
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
