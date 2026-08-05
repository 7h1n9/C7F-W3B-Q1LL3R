"""Diagnosis for bounded SQL Boolean Oracle experiments.

This module classifies durable output only.  It never promotes a fact and
never decides that a ToolCall succeeded merely because the Runner completed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import SolveRun
from app.models.solver_state import SolverState
from app.services.payload_strategy import payload_family, payload_strategy_manager


CLASSIFICATIONS = {
    "TRUE_SIDE_FAILED",
    "FALSE_SIDE_FAILED",
    "NO_DIFFERENCE",
    "NO_SIGNAL",
    "ORACLE_CONFIRMED",
}


def _bool(value: Any) -> bool:
    return value is True


def payload_fingerprint(payload: dict[str, Any]) -> str:
    contract = {
        "request_contract": payload.get("request_contract") or payload.get("request") or {},
        "test_field": payload.get("test_field"),
        "baseline_value": payload.get("baseline_value"),
        "true_condition": payload.get("true_condition"),
        "false_condition": payload.get("false_condition"),
        "oracle": payload.get("oracle") or {},
        "control_fields": payload.get("control_fields") or {},
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def diagnose_boolean_oracle(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic diagnosis for one Boolean Oracle result."""
    stable_true = _bool(payload.get("stable_true"))
    stable_false = _bool(payload.get("stable_false"))
    differential = _bool(payload.get("response_differential")) or _bool(payload.get("true_false_differential"))
    confirmed = _bool(payload.get("boolean_oracle_confirmed"))

    if confirmed and stable_true and stable_false and differential:
        classification = "ORACLE_CONFIRMED"
        next_action = "enter_calibration"
        strategy = "confirmed_oracle_calibration"
        reason = "TRUE and FALSE controls are stable and produce a confirmed response differential."
    elif not stable_true and stable_false:
        classification = "TRUE_SIDE_FAILED"
        next_action = "retry_true_condition"
        strategy = "repair_true_condition"
        reason = "The FALSE control is stable, but the TRUE condition does not produce the expected stable signal."
    elif stable_true and not stable_false:
        classification = "FALSE_SIDE_FAILED"
        next_action = "validate_negative_control"
        strategy = "repair_negative_control"
        reason = "The TRUE control is stable, but the FALSE control is not stable; the negative control must be validated."
    elif stable_true and stable_false and not differential:
        classification = "NO_DIFFERENCE"
        next_action = "change_signal_strategy"
        strategy = "change_response_signal"
        reason = "Both controls are stable, but their response signatures do not differ."
    else:
        classification = "NO_SIGNAL"
        next_action = "change_payload_family"
        strategy = "change_payload_family"
        reason = "Neither control forms a stable Boolean signal."

    confidence = 0.95 if classification == "ORACLE_CONFIRMED" else 0.9 if classification in {"TRUE_SIDE_FAILED", "FALSE_SIDE_FAILED"} else 0.85
    return {
        "classification": classification,
        "confidence": confidence,
        "reason": reason,
        "next_action": next_action,
        "recommended_strategy": strategy,
        "stable_true": stable_true,
        "stable_false": stable_false,
        "response_differential": differential,
        "boolean_oracle_confirmed": confirmed,
        "payload_fingerprint": payload_fingerprint(payload),
    }


class BooleanOracleDiagnosisService:
    async def record(self, session: AsyncSession, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        diagnosis = diagnose_boolean_oracle(payload)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run_id))
        if state is None:
            return diagnosis
        run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))

        timestamp = datetime.now(UTC).isoformat()
        history_entry = {
            "payload": {
                "request_contract": payload.get("request_contract") or payload.get("request") or {},
                "test_field": payload.get("test_field"),
                "baseline_value": payload.get("baseline_value"),
                "true_condition": payload.get("true_condition"),
                "false_condition": payload.get("false_condition"),
                "oracle": payload.get("oracle") or {},
                "control_fields": payload.get("control_fields") or {},
            },
            "classification": diagnosis["classification"],
            "timestamp": timestamp,
            "next_action": diagnosis["next_action"],
            "recommended_strategy": diagnosis["recommended_strategy"],
            "payload_fingerprint": diagnosis["payload_fingerprint"],
        }
        attack_history = list(state.attack_strategy_history_json or [])
        strategy_arguments = {
            "request": payload.get("request_contract") or payload.get("request") or {},
            "test_field": payload.get("test_field"),
            "baseline_value": payload.get("baseline_value"),
            "true_condition": payload.get("true_condition"),
            "false_condition": payload.get("false_condition"),
            "oracle": payload.get("oracle") or {},
            "control_fields": payload.get("control_fields") or {},
        }
        attack_entry = payload_strategy_manager.attack_strategy_entry(
            attack_history,
            vulnerability_type="SQL_INJECTION",
            target=str(payload.get("test_field") or ""),
            tool_name="sql_boolean_compare",
            payload_family_name=payload_family("sql_boolean_compare", payload),
            arguments=strategy_arguments,
            result="SUCCESS" if diagnosis["classification"] == "ORACLE_CONFIRMED" else "FAILURE",
            failure_reason=diagnosis["classification"],
        )
        state.attack_strategy_history_json = [*attack_history, attack_entry][-200:]
        ledger = dict(state.capability_ledger_json or {})
        history = [*(ledger.get("boolean_failure_history") or []), history_entry][-100:]
        state.capability_ledger_json = {**ledger, "boolean_failure_history": history}
        security_context = dict(state.security_context_json or {})
        security_context["boolean_diagnosis"] = {**diagnosis, "timestamp": timestamp}
        state.security_context_json = security_context
        if run is not None:
            checkpoint = dict(run.recovery_checkpoint_json or {})
            checkpoint["boolean_diagnosis"] = {**diagnosis, "timestamp": timestamp}
            checkpoint["do_not_repeat_boolean_payload_fingerprint"] = diagnosis["payload_fingerprint"]
            run.recovery_checkpoint_json = checkpoint
        await session.flush()
        return diagnosis


boolean_oracle_diagnosis_service = BooleanOracleDiagnosisService()
