"""Deterministic State-driven Solver Loop transition engine."""

from __future__ import annotations

from typing import Any, Mapping, Iterable

from app.security.attack_state import AttackState
from app.services.strategy_continuation import normalize_strategy, strategy_identity


_ACTION_TO_STRATEGY = {
    "BOOLEAN_COMMENT_HASH": "BOOLEAN_AND_COMMENT_HASH",
    "BOOLEAN_COMMENT_INLINE": "BOOLEAN_AND_COMMENT_INLINE",
    "BOOLEAN_ENCODING": "BOOLEAN_AND_ENCODING",
    "BOOLEAN_OR": "BOOLEAN_OR",
    "ERROR_BASED": "ERROR_BASED",
    "UNION_BASED": "UNION",
    "TIME_BASED": "TIME_BASED",
}
_STRATEGY_TO_ACTION = {value: key for key, value in _ACTION_TO_STRATEGY.items()}


def strategy_to_action(strategy: Any) -> str:
    canonical = normalize_strategy(strategy)
    return _STRATEGY_TO_ACTION.get(canonical, canonical)


def action_to_strategy(action: Any) -> str:
    token = str(action or "").strip().upper().replace("-", "_")
    return _ACTION_TO_STRATEGY.get(token, normalize_strategy(token))


def _text(value: Any) -> str:
    return str(value or "").strip().upper()


def _status_success(value: Any) -> bool:
    return _text(value) in {"VALIDATED", "SUCCESS", "CONFIRMED", "CREATED"}


class AttackStateEngine:
    """Pure state transition rules; it never executes a tool or stores evidence."""

    def evaluate(
        self,
        security_context: Mapping[str, Any] | None = None,
        strategy_history: Iterable[Mapping[str, Any]] | None = None,
        diagnosis: Mapping[str, Any] | None = None,
        *,
        target: Mapping[str, Any] | None = None,
        current_phase: str = "HYPOTHESIS",
    ) -> AttackState:
        context = security_context or {}
        history = [dict(item) for item in (strategy_history or []) if isinstance(item, Mapping)]
        diagnosis_data = dict(diagnosis or {})
        latest = history[-1] if history else {}
        vulnerability_type = _text(diagnosis_data.get("vulnerability_type") or latest.get("vulnerability_type") or "SQL_INJECTION")
        current_source = diagnosis_data.get("current_strategy") if isinstance(diagnosis_data.get("current_strategy"), Mapping) else diagnosis_data
        current = strategy_identity(current_source)
        current = current or normalize_strategy(diagnosis_data.get("strategy"))
        current = current or strategy_identity(latest)
        current_family = _text(diagnosis_data.get("strategy_family") or latest.get("strategy_family"))
        current_variant = _text(diagnosis_data.get("strategy_variant") or latest.get("strategy_variant"))
        if not current_family and current.startswith("BOOLEAN_"):
            current_family = "BOOLEAN"
        if not current_variant and current.startswith("BOOLEAN_"):
            current_variant = current.removeprefix("BOOLEAN_")
        failed = []
        for entry in history:
            identity = strategy_identity(entry)
            if identity and (_text(entry.get("status") or entry.get("result")) not in {"CONFIRMED", "COMPLETED", "SUCCESS"}):
                failed.append(identity)
        failed = list(dict.fromkeys(failed))
        blocked = list(dict.fromkeys(failed))

        validation = [item for item in (context.get("validation_results") or []) if isinstance(item, Mapping)]
        if any(_status_success(item.get("status")) for item in validation):
            return AttackState(
                vulnerability_type=vulnerability_type,
                target=dict(target or {}),
                current_phase="EXPLOITATION",
                current_strategy_family=current_family,
                current_strategy_variant=current_variant,
                attempt_history=history[-20:],
                failed_strategies=failed,
                blocked_strategies=blocked,
                available_actions=["METADATA_EXTRACTION", "DATA_EXTRACTION"],
                required_transition="BEGIN_EXPLOITATION",
                transition_reason="VALIDATION_CONFIRMED",
                confidence=max((float(item.get("confidence") or 0.0) for item in validation), default=0.95),
            )

        classification = _text(diagnosis_data.get("classification"))
        boolean_attempts = sum(1 for item in history if _text(item.get("strategy_family")) == "BOOLEAN" or strategy_identity(item).startswith("BOOLEAN_"))
        recommended = [normalize_strategy(item) for item in (diagnosis_data.get("recommended_strategy") or diagnosis_data.get("recommended_strategies") or [])]
        boolean_exhausted = bool(diagnosis_data.get("family_exhausted") or diagnosis_data.get("exhausted")) or boolean_attempts >= 3
        if classification in {"TRUE_SIDE_FAILED", "FALSE_SIDE_FAILED", "NO_DIFFERENCE", "NO_SIGNAL"}:
            if boolean_exhausted:
                actions = ["ERROR_BASED", "UNION_BASED", "TIME_BASED"]
                transition = "CHANGE_ATTACK_FAMILY"
                reason = "BOOLEAN_FAMILY_EXHAUSTED"
            else:
                if classification == "TRUE_SIDE_FAILED":
                    candidates = ["BOOLEAN_AND_COMMENT_HASH", "BOOLEAN_AND_ENCODING"]
                elif classification == "NO_DIFFERENCE":
                    candidates = ["ERROR_BASED", "TIME_BASED"]
                elif classification == "FALSE_SIDE_FAILED":
                    candidates = ["BOOLEAN_OR"]
                else:
                    candidates = recommended or [
                        "BOOLEAN_AND_COMMENT_HASH",
                        "BOOLEAN_AND_ENCODING",
                        "BOOLEAN_OR",
                    ]
                actions = [strategy_to_action(item) for item in candidates]
                actions = [item for item in dict.fromkeys(actions) if action_to_strategy(item) not in blocked]
                transition = "CHANGE_BOOLEAN_VARIANT"
                reason = classification
            return AttackState(
                vulnerability_type=vulnerability_type,
                target=dict(target or {}),
                current_phase="BOOLEAN_CALIBRATION",
                current_strategy_family=current_family or "BOOLEAN",
                current_strategy_variant=current_variant or current,
                attempt_history=history[-20:],
                failed_strategies=failed,
                blocked_strategies=blocked,
                available_actions=actions,
                required_transition=transition,
                transition_reason=reason,
                confidence=float(diagnosis_data.get("confidence") or 0.8),
            )

        return AttackState(
            vulnerability_type=vulnerability_type,
            target=dict(target or {}),
            current_phase=str(current_phase or "HYPOTHESIS").upper(),
            current_strategy_family=current_family,
            current_strategy_variant=current_variant,
            attempt_history=history[-20:],
            failed_strategies=failed,
            blocked_strategies=blocked,
            available_actions=["BOOLEAN_AND"] if vulnerability_type == "SQL_INJECTION" else [],
            required_transition="INITIALIZE_STRATEGY" if vulnerability_type == "SQL_INJECTION" else None,
            transition_reason="INITIAL_STATE",
            confidence=0.4,
        )

    # Compatibility aliases make the engine easy to use from controller and
    # focused tests without introducing another state service abstraction.
    compute = evaluate
    update = evaluate


def validate_attack_state_selection(
    attack_state: Mapping[str, Any] | AttackState | None,
    selection: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    state = attack_state.model_dump(mode="json") if isinstance(attack_state, AttackState) else dict(attack_state or {})
    if isinstance(selection, Mapping):
        selected = strategy_identity(selection)
    else:
        selected = normalize_strategy(selection)
    allowed = [action_to_strategy(item) for item in (state.get("available_actions") or [])]
    if not allowed:
        return {"valid": True, "selected_strategy": selected or None, "reason": "NO_ATTACK_STATE_RESTRICTION"}
    if selected not in allowed:
        return {"valid": False, "selected_strategy": selected or None, "allowed_actions": list(state.get("available_actions") or []), "reason": "ATTACK_ACTION_NOT_ALLOWED"}
    return {"valid": True, "selected_strategy": selected, "allowed_actions": list(state.get("available_actions") or []), "reason": "ATTACK_ACTION_ALLOWED"}


attack_state_engine = AttackStateEngine()
