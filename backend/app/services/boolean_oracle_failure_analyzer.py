"""Explain why a bounded Boolean Oracle side failed.

The analyzer is intentionally evidence-only.  It does not promote a fact or
declare a vulnerability; it gives the Controller a bounded set of payload
variants that are materially different from the failed request.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _signature_value(signature: Mapping[str, Any], oracle: Mapping[str, Any]) -> Any:
    field = str(oracle.get("json_field") or "matched")
    return signature.get(field)


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    contract = {
        "request_contract": payload.get("request_contract") or payload.get("request") or {},
        "test_field": payload.get("test_field"),
        "baseline_value": payload.get("baseline_value"),
        "true_condition": payload.get("true_condition"),
        "false_condition": payload.get("false_condition"),
        "oracle": payload.get("oracle") or {},
        "control_fields": payload.get("control_fields") or {},
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def analyze_boolean_oracle(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a stable diagnosis and bounded strategy recommendations."""
    data = dict(payload or {})
    oracle = _mapping(data.get("oracle"))
    true_signature = _mapping(data.get("true_signature"))
    false_signature = _mapping(data.get("false_signature"))
    if not true_signature and isinstance(data.get("true_results"), list):
        first_true = data["true_results"][0] if data["true_results"] else {}
        true_signature = _mapping(first_true.get("signature")) if isinstance(first_true, Mapping) else {}
    if not false_signature and isinstance(data.get("false_results"), list):
        first_false = data["false_results"][0] if data["false_results"] else {}
        false_signature = _mapping(first_false.get("signature")) if isinstance(first_false, Mapping) else {}
    stable_true = data.get("stable_true") is True
    stable_false = data.get("stable_false") is True
    differential = (
        data.get("response_differential") is True
        or data.get("true_false_differential") is True
    )
    confirmed = data.get("boolean_oracle_confirmed") is True

    if confirmed and stable_true and stable_false and differential:
        classification = "ORACLE_CONFIRMED"
    elif not stable_true and stable_false:
        classification = "TRUE_SIDE_FAILED"
    elif stable_true and not stable_false:
        classification = "FALSE_SIDE_FAILED"
    elif stable_true and stable_false and not differential:
        classification = "NO_DIFFERENCE"
    else:
        classification = "NO_SIGNAL"

    reasons: list[str] = []
    recommendations: list[str] = []
    true_condition = str(data.get("true_condition") or "")
    false_condition = str(data.get("false_condition") or "")

    if classification == "TRUE_SIDE_FAILED":
        expected_true = oracle.get("true_value", True)
        actual_true = _signature_value(true_signature, oracle)
        if not true_signature:
            reasons.append("true_signature_missing")
        elif actual_true != expected_true:
            reasons.append("payload_not_reflected")
        if not true_condition:
            reasons.append("true_condition_missing")
        if re.search(r"--\s*$", true_condition):
            reasons.append("comment_failed")
        if "'" in true_condition:
            reasons.append("quote_break")
        if not reasons:
            reasons.append("true_signal_unstable")
        if "comment_failed" in reasons:
            recommendations.extend(["BOOLEAN_AND_COMMENT_HASH", "BOOLEAN_AND_COMMENT_INLINE"])
        if "quote_break" in reasons:
            recommendations.append("BOOLEAN_AND_ENCODING")
        recommendations.append("BOOLEAN_OR")
    elif classification == "FALSE_SIDE_FAILED":
        reasons.append("negative_control_unstable")
        recommendations.append("VALIDATE_NEGATIVE_CONTROL")
    elif classification == "NO_DIFFERENCE":
        reasons.append("response_signal_same")
        recommendations.extend(["ERROR_BASED", "TIME_BASED"])
    elif classification == "NO_SIGNAL":
        reasons.extend(["true_signal_unstable", "false_signal_unstable"])
        recommendations.extend(["BOOLEAN_OR", "ERROR_BASED"])
    else:
        reasons.append("stable_differential_confirmed")

    if classification == "ORACLE_CONFIRMED":
        next_action = "enter_validation_result"
    elif classification == "TRUE_SIDE_FAILED":
        next_action = "retry_true_condition"
    elif classification == "FALSE_SIDE_FAILED":
        next_action = "validate_negative_control"
    elif classification == "NO_DIFFERENCE":
        next_action = "change_signal_strategy"
    else:
        next_action = "change_payload_family"

    confidence = {
        "ORACLE_CONFIRMED": 0.95,
        "TRUE_SIDE_FAILED": 0.90,
        "FALSE_SIDE_FAILED": 0.80,
        "NO_DIFFERENCE": 0.85,
        "NO_SIGNAL": 0.75,
    }[classification]
    return {
        "classification": classification,
        "confidence": confidence,
        "reason": _dedupe(reasons),
        "reason_text": "; ".join(_dedupe(reasons)),
        "next_action": next_action,
        "recommended_strategy": _dedupe(recommendations),
        "recommended_strategies": _dedupe(recommendations),
        "stable_true": stable_true,
        "stable_false": stable_false,
        "response_differential": differential,
        "boolean_oracle_confirmed": confirmed,
        "payload_fingerprint": _payload_fingerprint(data),
        "true_observed_value": _signature_value(true_signature, oracle),
        "false_observed_value": _signature_value(false_signature, oracle),
    }


def apply_boolean_strategy_variant(arguments: Mapping[str, Any], strategy: str) -> dict[str, Any]:
    """Materialize one migration recommendation into Boolean conditions."""
    result = dict(arguments or {})
    token = str(strategy or "").upper()
    if not token:
        return result

    def normalize_operator(condition: str, operator: str = "AND") -> str:
        return re.sub(r"\b(?:AND|OR)\b", operator, condition, count=1, flags=re.IGNORECASE)

    def condition(name: str) -> str:
        return str(result.get(name) or "")

    if token == "BOOLEAN_OR":
        result["true_condition"] = normalize_operator(condition("true_condition"), "OR")
        result["false_condition"] = normalize_operator(condition("false_condition"), "OR")
    elif token == "BOOLEAN_AND_COMMENT_HASH":
        for name in ("true_condition", "false_condition"):
            value = normalize_operator(condition(name), "AND")
            value = re.sub(r"--\s*$|#\s*$", "", value).rstrip() + "#"
            result[name] = value
    elif token == "BOOLEAN_AND_COMMENT_INLINE":
        for name in ("true_condition", "false_condition"):
            value = normalize_operator(condition(name), "AND")
            value = re.sub(r"--\s*$|#\s*$", "", value).rstrip()
            value = re.sub(r"\bAND\s+", "AND/**/", value, count=1, flags=re.IGNORECASE)
            result[name] = value + "#"
    elif token == "BOOLEAN_AND_ENCODING":
        for name in ("true_condition", "false_condition"):
            value = normalize_operator(condition(name), "AND")
            result[name] = value.replace("'", "%27").replace(" ", "%20")
    return result
