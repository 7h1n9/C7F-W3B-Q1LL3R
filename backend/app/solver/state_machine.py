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
        SolverPhase.EXPLOITATION: ("oracle_expression_calibration", "mysql_metadata_discovery", "sql_extract"),
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
        if (
            phase is SolverPhase.EXPLOITATION
            and not state.knowledge.get("target_url")
            and not state.control.get("tested_parameter")
            and not state.control.get("validation_status")
        ):
            # Phase 1.1 Blackboard fixtures did not carry target context.
            # Preserve their original compatibility surface; production v2
            # states always have a target and validated control field.
            return ["mysql_metadata_discovery", "sql_extract"]
        actions = list(self.ACTIONS[phase])
        # Keep the normal Phase 1 action surface stable. After repeated
        # bounded metadata NO_FACT results, expose the existing automation
        # tools as a state-driven fallback.
        if phase is SolverPhase.EXPLOITATION and int(state.control.get("metadata_failures") or 0) >= 2:
            actions.extend(("request_capture", "sqlmap_detect", "sqlmap_run", "sqlite_metadata_discovery", "script_run"))
        return actions

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
