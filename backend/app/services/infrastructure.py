"""Control-plane failure classification and the Run infrastructure circuit."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.exceptions import DomainError

CONTROL_PLANE_CODES = {
    "TARGET_UNAVAILABLE",
    "BACKEND_UNAVAILABLE",
    "BACKEND_PERSISTENCE_FAILED",
    "MCP_VALIDATION_FAILED",
    "RUNNER_UNAVAILABLE",
    "COMPACTION_FAILED",
    "TOOL_RESULT_DELIVERY_FAILED",
}
INFRASTRUCTURE_STATES = {"INFRASTRUCTURE_VALIDATION", "WAITING_CONFIGURATION"}


def is_control_plane_error(code: str | None) -> bool:
    return str(code or "") in CONTROL_PLANE_CODES


def infrastructure_error(code: str | None, stage: str | None = None) -> bool:
    return is_control_plane_error(code) or str(stage or "").upper() in {"BACKEND", "RUNNER", "MCP", "TRACE_WRITE", "ARTIFACT_DOWNLOAD"}


def record_failure(run, *, code: str, message: str, stage: str = "INFRASTRUCTURE") -> int:
    """Advance the circuit without touching solver no-progress counters."""
    streak = int(getattr(run, "infrastructure_error_streak", 0) or 0) + 1
    run.infrastructure_error_streak = streak
    run.infrastructure_state = "WAITING_CONFIGURATION" if streak >= 3 else "INFRASTRUCTURE_VALIDATION" if streak >= 2 else "HEALTHY"
    run.infrastructure_last_error_json = {
        "code": code,
        "message": message[:2000],
        "stage": stage,
        "at": datetime.now(UTC).isoformat(),
    }
    if streak >= 2 and str(run.status) not in {"COMPLETED_SOLVED", "CANCELLED"}:
        run.status = "WAITING_CONFIGURATION" if streak >= 3 else "INFRASTRUCTURE_VALIDATION"
        run.current_phase = run.status
        run.last_error_code = code
        run.last_error_message = message[:2000]
    return streak


def clear_failure(run) -> None:
    run.infrastructure_error_streak = 0
    run.infrastructure_state = "HEALTHY"
    run.infrastructure_last_error_json = {}


def reject_infrastructure(run) -> None:
    raise DomainError(
        "INFRASTRUCTURE_VALIDATION",
        "Target tools are paused while the control plane is being validated.",
        {"infrastructure_state": getattr(run, "infrastructure_state", "INFRASTRUCTURE_VALIDATION"), "error": getattr(run, "infrastructure_last_error_json", {})},
        503,
        stage="INFRASTRUCTURE",
        retryable=True,
    )
