from __future__ import annotations

from enum import StrEnum

from .blackboard.models import BlackboardState


class SolverPhase(StrEnum):
    BASELINE = "BASELINE"
    VALIDATION = "VALIDATION"
    EXPLOITATION = "EXPLOITATION"
    IMPACT = "IMPACT"
    REPORTING = "REPORTING"


class TaskStateMachine:
    """Phase-scoped action policy for the new Solver Core skeleton."""

    ACTIONS: dict[SolverPhase, tuple[str, ...]] = {
        SolverPhase.BASELINE: ("http_request",),
        SolverPhase.VALIDATION: ("sql_boolean_compare", "oracle_probe_matrix"),
        SolverPhase.EXPLOITATION: ("mysql_metadata_discovery", "sql_extract"),
        SolverPhase.IMPACT: ("impact_validation",),
        SolverPhase.REPORTING: ("report",),
    }

    NEXT_PHASE: dict[SolverPhase, SolverPhase] = {
        SolverPhase.BASELINE: SolverPhase.VALIDATION,
        SolverPhase.VALIDATION: SolverPhase.EXPLOITATION,
        SolverPhase.EXPLOITATION: SolverPhase.IMPACT,
        SolverPhase.IMPACT: SolverPhase.REPORTING,
        SolverPhase.REPORTING: SolverPhase.REPORTING,
    }

    def allowed_actions(self, state: BlackboardState) -> list[str]:
        """Return actions admissible for the current phase.

        The phase policy is the source of allowed actions.  A Blackboard
        snapshot may carry a cached list, but the skeleton does not trust a
        stale list over this state-machine definition.
        """
        try:
            phase = SolverPhase(state.phase)
        except ValueError:
            return []
        return list(self.ACTIONS[phase])

    def is_allowed(self, state: BlackboardState, action: str) -> bool:
        return action in self.allowed_actions(state)

    def next_phase(self, state: BlackboardState, action: str, status: str) -> str:
        """Advance only after a successful action from the current phase."""
        if str(status or "").upper() not in {"SUCCESS", "COMPLETED", "OK"}:
            return state.phase
        if not self.is_allowed(state, action):
            return state.phase
        try:
            return self.NEXT_PHASE[SolverPhase(state.phase)].value
        except (KeyError, ValueError):
            return state.phase
