import hashlib
import json


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint_action(tool_name: str, arguments: dict) -> str:
    payload = f"{tool_name}:{canonical_json(arguments)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_compiled_action(tool_name: str, compiled_arguments_digest: str, success_condition: str, stage: str = "") -> str:
    payload = f"{tool_name}:{stage.strip().upper()}:{compiled_arguments_digest}:{success_condition.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
