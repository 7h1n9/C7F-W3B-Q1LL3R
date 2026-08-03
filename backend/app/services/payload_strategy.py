"""Durable bounded payload-family switching for one Run."""

from __future__ import annotations

import hashlib
import json


PAYLOAD_FAMILIES = (
    "boolean_compare", "scalar_subquery", "substring", "hex", "length",
    "exists", "count", "like", "wrapper_variant", "comment_variant",
)


def payload_family(tool_name: str, arguments: dict) -> str:
    explicit = str(arguments.get("payload_family") or "").strip().lower()
    if explicit in PAYLOAD_FAMILIES:
        return explicit
    expression = str(arguments.get("target_expression") or arguments.get("true_condition") or "").lower()
    for family, markers in {
        "scalar_subquery": ("select", "("), "substring": ("substring", "substr"),
        "hex": ("hex(",), "length": ("length(",), "exists": ("exists",),
        "count": ("count(",), "like": (" like ",), "wrapper_variant": ("wrapper",),
        "comment_variant": ("--", "/*"),
    }.items():
        if any(marker in expression for marker in markers):
            return family
    return "boolean_compare" if tool_name == "sql_boolean_compare" else "scalar_subquery"


def arguments_digest(arguments: dict) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class PayloadStrategyManager:
    def record(self, checkpoint: dict, *, tool_name: str, stage: str, arguments: dict,
               error_code: str, confidence: float | None, result: str) -> dict:
        family = payload_family(tool_name, arguments)
        digest = arguments_digest(arguments)
        history = list(checkpoint.get("payload_strategy_history") or [])
        matches = [item for item in history if item.get("tool_name") == tool_name and item.get("stage") == stage and item.get("payload_family") == family and item.get("arguments_digest") == digest and item.get("error_code") == error_code]
        count = len(matches) + 1
        entry = {"tool_name": tool_name, "stage": stage, "payload_family": family, "expression": arguments.get("target_expression") or arguments.get("true_condition"), "target": arguments.get("target_expression") or arguments.get("test_field"), "arguments_digest": digest, "error_code": error_code, "confidence": confidence, "result": result, "count": count}
        history.append(entry)
        entry["status"] = "BLOCKED" if count >= 2 else "OPTIMIZE_ONCE"
        checkpoint["payload_strategy_history"] = history[-200:]
        return entry

    def is_blocked(self, checkpoint: dict, *, tool_name: str, stage: str, arguments: dict, error_code: str) -> bool:
        digest = arguments_digest(arguments)
        return any(item.get("tool_name") == tool_name and item.get("stage") == stage and item.get("payload_family") == payload_family(tool_name, arguments) and item.get("arguments_digest") == digest and item.get("error_code") == error_code and item.get("status") == "BLOCKED" for item in checkpoint.get("payload_strategy_history") or [])

    def has_unexhausted_family(self, checkpoint: dict, *, tool_name: str, stage: str) -> bool:
        blocked = {item.get("payload_family") for item in checkpoint.get("payload_strategy_history") or [] if item.get("tool_name") == tool_name and item.get("stage") == stage and item.get("status") == "BLOCKED"}
        return len(blocked) < len(PAYLOAD_FAMILIES)


payload_strategy_manager = PayloadStrategyManager()
