import hashlib
import json
from typing import Any


STRATEGY_METADATA_KEYS = {
    "vulnerability_type",
    "strategy_family",
    "strategy_variant",
    "signal_type",
    "encoding",
    "payload_family",
}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def normalize_request(arguments: dict | None) -> dict[str, Any]:
    """Normalize both ordinary HTTP and nested tool request contracts."""
    args = dict(arguments or {})
    nested = args.get("request") if isinstance(args.get("request"), dict) else {}
    method = nested.get("method") or args.get("method") or ""
    url = nested.get("url") or nested.get("endpoint") or args.get("url") or args.get("endpoint") or ""
    raw_json = nested.get("json") if "json" in nested else args.get("json")
    raw_body = nested.get("body") if "body" in nested else args.get("body")
    json_body = _json_value(raw_json)
    body = _json_value(raw_body)
    # A JSON request body is represented once under json.  Plain body payloads
    # remain under body so two materially different requests cannot collide.
    if json_body is not None:
        body = None
    return {
        "method": str(method or "").upper(),
        "url": str(url or ""),
        "endpoint": str(url or ""),
        "body": body,
        "json": json_body,
    }


def normalize_payload(arguments: dict | None) -> dict[str, Any]:
    """Keep attack semantics outside the transport request identity."""
    args = dict(arguments or {})
    return {
        str(key): _json_value(value)
        for key, value in sorted(args.items())
        if key not in {"request", "method", "url", "endpoint", "body", "json"}
        and key not in STRATEGY_METADATA_KEYS
    }


def build_execution_fingerprint(
    tool_name: str,
    arguments: dict | None,
    *,
    stage: str = "",
) -> str:
    """Fingerprint one exact controller execution, including nested payloads."""
    payload = {
        "version": "execution-v2",
        "tool_name": str(tool_name or ""),
        "stage": str(stage or "").strip().upper(),
        "request": normalize_request(arguments),
        "payload": normalize_payload(arguments),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_strategy_fingerprint(
    *,
    vulnerability_type: str,
    target: dict | None,
    strategy_family: str,
    strategy_variant: str = "",
    signal_type: str = "",
    encoding: str = "",
    payload_family: str = "",
) -> str:
    """Fingerprint an attack strategy without hypothesis prose or literals."""
    normalized_target = dict(target or {})
    payload = {
        "version": "strategy-v2",
        "vulnerability_type": str(vulnerability_type or "").upper(),
        "target": {
            "endpoint": str(normalized_target.get("endpoint") or normalized_target.get("url") or ""),
            "parameter": str(normalized_target.get("parameter") or ""),
        },
        "family": str(strategy_family or "").upper(),
        "variant": str(strategy_variant or "").upper(),
        "signal": str(signal_type or "").upper(),
        "encoding": str(encoding or "").upper(),
        "payload_family": str(payload_family or "").upper(),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def fingerprint_action(tool_name: str, arguments: dict) -> str:
    payload = f"{tool_name}:{canonical_json(arguments)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_compiled_action(tool_name: str, compiled_arguments_digest: str, success_condition: str, stage: str = "") -> str:
    payload = f"{tool_name}:{stage.strip().upper()}:{compiled_arguments_digest}:{success_condition.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
