from dataclasses import dataclass


@dataclass(frozen=True)
class ActionQualityDecision:
    quality: str
    action: str
    reason: str
    streak: int


class RecoveryPlanner:
    def plan(self, *, phase: str, no_progress: int, duplicate_streak: int) -> dict:
        if duplicate_streak >= 3:
            return {"action": "AutomationAction", "reason": "Repeated identical actions require a bounded automation experiment."}
        if no_progress >= 6:
            return {"action": "PlanAction", "reason": "Switch to another attack-chain node."}
        if no_progress >= 4:
            return {"action": "PlanAction", "reason": "Switch experiment dimension."}
        return {"action": "PlanAction", "reason": "Make the missing decision explicit."}


class ActionQualityGate:
    """Reject default filler actions once evidence exists without a plan."""

    def evaluate(self, action: dict, state: dict | None = None) -> ActionQualityDecision:
        state = state or {}
        degraded = (
            action.get("type") == "tool"
            and action.get("objective") == "Continue the authorized investigation"
            and (not action.get("hypothesis") or action.get("hypothesis") == "Initial investigation hypothesis")
            and str(state.get("current_phase") or "INTAKE").upper() == "INTAKE"
            and bool(state.get("confirmed_facts") or state.get("has_evidence"))
            and not state.get("plan_node_id")
            and not state.get("decision_question")
        )
        if not degraded:
            return ActionQualityDecision("ACCEPT", "", "Action contains a concrete objective and decision context.", 0)
        streak = int(state.get("degraded_action_streak") or 0) + 1
        if streak == 1:
            return ActionQualityDecision("DEGRADED", "REPAIR_ACTION", "Repair objective, hypothesis, plan node and decision question.", streak)
        if streak == 2:
            return ActionQualityDecision("REJECT", "PlanAction", "Repeated degraded actions are blocked until a plan is supplied.", streak)
        return ActionQualityDecision("REJECT", "RecoveryPlanner", "RecoveryPlanner must select a new experiment dimension.", streak)


action_quality_gate = ActionQualityGate()
recovery_planner = RecoveryPlanner()
