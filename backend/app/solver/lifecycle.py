"""Pure lifecycle projection for Solver v2 outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.orchestration.state_machine import RunStatus


class SolverLifecycleOutcome(StrEnum):
    """A Solver-owned outcome before it is projected onto RunStatus."""

    CONTINUE = "CONTINUE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    RECOVERABLE_WORKER_FAILURE = "RECOVERABLE_WORKER_FAILURE"
    ENGINE_EXCEPTION = "ENGINE_EXCEPTION"
    TIMEOUT = "TIMEOUT"
    COMPLETION_SOLVED = "COMPLETION_SOLVED"
    COMPLETION_UNSOLVED = "COMPLETION_UNSOLVED"


@dataclass(frozen=True)
class LifecycleDecision:
    """The status projection returned to the runtime boundary."""

    target_status: RunStatus
    reason_code: str


class SolverLifecycleMapper:
    """Map Solver outcomes without mutating or inspecting runtime state.

    ``COMPLETION_SOLVED`` is deliberately only a symbolic input here.  The
    caller must obtain that outcome from the Solver Completion Gate; this
    mapper neither evaluates findings nor has access to Evidence authority.
    """

    _MAPPINGS: dict[SolverLifecycleOutcome, RunStatus] = {
        SolverLifecycleOutcome.CONTINUE: RunStatus.RUNNING,
        SolverLifecycleOutcome.APPROVAL_REQUIRED: RunStatus.WAITING_USER,
        SolverLifecycleOutcome.USER_INPUT_REQUIRED: RunStatus.WAITING_USER,
        SolverLifecycleOutcome.RECOVERABLE_WORKER_FAILURE: RunStatus.RUNNING,
        SolverLifecycleOutcome.ENGINE_EXCEPTION: RunStatus.FAILED_ENGINE,
        SolverLifecycleOutcome.TIMEOUT: RunStatus.TIMEOUT,
        SolverLifecycleOutcome.COMPLETION_SOLVED: RunStatus.COMPLETED_SOLVED,
        SolverLifecycleOutcome.COMPLETION_UNSOLVED: RunStatus.COMPLETED_UNSOLVED,
    }

    def map(self, outcome: SolverLifecycleOutcome) -> LifecycleDecision:
        """Return a status projection; state transitions remain external."""

        normalized = SolverLifecycleOutcome(outcome)
        return LifecycleDecision(
            target_status=self._MAPPINGS[normalized],
            reason_code=normalized.value,
        )


__all__ = [
    "LifecycleDecision",
    "SolverLifecycleMapper",
    "SolverLifecycleOutcome",
]
