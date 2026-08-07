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
