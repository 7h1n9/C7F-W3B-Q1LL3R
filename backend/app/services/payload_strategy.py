"""Durable bounded payload-family switching for one Run."""

from __future__ import annotations

import hashlib
import json


PAYLOAD_FAMILIES = (
    "and_boolean", "or_boolean", "comment_variation", "whitespace_variation",
    "encoding_variation", "substring", "ascii", "hex", "like", "scalar_subquery",
    "length", "exists", "count", "wrapper_variant",
)


def payload_family(tool_name: str, arguments: dict) -> str:
    explicit = str(arguments.get("payload_family") or "").strip().lower()
    if explicit in PAYLOAD_FAMILIES:
        return explicit
    expression = str(arguments.get("target_expression") or arguments.get("true_condition") or "").lower()
    if tool_name == "sql_boolean_compare":
        if "or" in expression and " and " not in expression:
            return "or_boolean"
        if "%27" in expression or "char(" in expression or "0x" in expression:
            return "encoding_variation"
        if "--" in expression or "/*" in expression or "#" in expression:
            return "comment_variation"
        if "  " in expression or "\t" in expression or "\n" in expression:
            return "whitespace_variation"
        return "and_boolean"
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

    def attack_strategy_entry(
        self,
        history: list[dict],
        *,
        vulnerability_type: str,
        target: str,
        tool_name: str,
        payload_family_name: str,
        arguments: dict,
        result: str,
        failure_reason: str,
    ) -> dict:
        digest = arguments_digest(arguments)
        matches = [item for item in history if (
            item.get("vulnerability_type") == vulnerability_type
            and item.get("target") == target
            and item.get("payload_fingerprint") == digest
        )]
        attempts = len(matches) + 1
        return {
            "vulnerability_type": vulnerability_type,
            "target": target,
            "tool_name": tool_name,
            "payload_family": payload_family_name,
            "payload_fingerprint": digest,
            "result": result,
            "failure_reason": failure_reason,
            "attempts": attempts,
            "status": "BLOCKED" if result == "FAILURE" and attempts >= 2 else "AVAILABLE",
        }

    def is_attack_strategy_blocked(self, history: list[dict], *, vulnerability_type: str, target: str, arguments: dict) -> bool:
        digest = arguments_digest(arguments)
        return any(
            item.get("vulnerability_type") == vulnerability_type
            and item.get("target") == target
            and item.get("payload_fingerprint") == digest
            and item.get("result") == "FAILURE"
            and int(item.get("attempts") or 0) >= 2
            for item in history
        )

    def failed_strategies(self, history: list[dict]) -> list[dict]:
        return [item for item in history if item.get("result") == "FAILURE"]


payload_strategy_manager = PayloadStrategyManager()
