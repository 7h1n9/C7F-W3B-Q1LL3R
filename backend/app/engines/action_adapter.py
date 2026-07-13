import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.schemas.agent import AgentAction

ACTION_ADAPTER = TypeAdapter(AgentAction)


@dataclass
class NormalizationTrace:
    original_shape: str
    normalized_fields: list[str] = field(default_factory=list)
    normalization_count: int = 0
    validation_result: str = "pending"
    normalization_quality: str = "STRICT"

    def mark(self, field: str, *, degraded: bool = False) -> None:
        self.normalized_fields.append(field)
        self.normalization_count += 1
        if degraded:
            self.normalization_quality = "DEGRADED"


TYPE_ALIASES = {
    "ToolAction": "tool",
    "tool_action": "tool",
    "TOOL_ACTION": "tool",
    "tool": "tool",
    "SkillAction": "skill",
    "skill_action": "skill",
    "SKILL_ACTION": "skill",
    "skill": "skill",
    "FinishAction": "finish",
    "finish_action": "finish",
    "FINISH_ACTION": "finish",
    "finish": "finish",
}

FIELD_ALIASES = {
    "tool": "tool_name",
    "toolName": "tool_name",
    "params": "arguments",
    "args": "arguments",
    "active_hypothesis": "hypothesis",
    "tool_reason": "reason",
    "action_reason": "reason",
    "evidence": "supporting_evidence",
}


def _shape(value: Any) -> str:
    if isinstance(value, dict):
        return "object:" + ",".join(sorted(str(key) for key in value.keys())[:20])
    return type(value).__name__


def _copy_known_aliases(payload: dict[str, Any], trace: NormalizationTrace) -> dict[str, Any]:
    normalized = dict(payload)
    for alias, target in FIELD_ALIASES.items():
        if alias in normalized and target not in normalized:
            normalized[target] = normalized[alias]
            trace.mark(f"{alias}->{target}")
        if alias in normalized and alias != target:
            normalized.pop(alias, None)
    return normalized


def normalize_action_payload(raw_action: dict[str, Any]) -> tuple[dict[str, Any], NormalizationTrace]:
    trace = NormalizationTrace(original_shape=_shape(raw_action))
    normalized = dict(raw_action)

    if "tool" in normalized and isinstance(normalized["tool"], dict):
        nested = dict(normalized.pop("tool"))
        nested.setdefault("type", "tool")
        normalized = {**nested, **normalized}
        trace.mark("tool-object->tool-action")
    elif "skill" in normalized and isinstance(normalized["skill"], dict):
        nested = dict(normalized.pop("skill"))
        nested.setdefault("type", "skill")
        normalized = {**nested, **normalized}
        trace.mark("skill-object->skill-action")
    elif "finish" in normalized and isinstance(normalized["finish"], dict):
        nested = dict(normalized.pop("finish"))
        nested.setdefault("type", "finish")
        normalized = {**nested, **normalized}
        trace.mark("finish-object->finish-action")

    action_type = normalized.get("type") or normalized.get("action_type")
    if action_type is None:
        if normalized.get("tool_name") or normalized.get("tool") or "arguments" in normalized or "args" in normalized or "params" in normalized:
            action_type = "tool"
            trace.mark("infer-type:tool")
        elif normalized.get("operation") and (
            normalized.get("skill_id")
            or normalized.get("skill_name")
            or normalized.get("skill_identity")
        ):
            action_type = "skill"
            trace.mark("infer-type:skill")
        elif normalized.get("result") is not None or normalized.get("flag_candidate") is not None:
            action_type = "finish"
            trace.mark("infer-type:finish")
    if isinstance(action_type, str):
        mapped = TYPE_ALIASES.get(action_type, TYPE_ALIASES.get(action_type.lower()))
        if mapped:
            if mapped != normalized.get("type"):
                trace.mark(f"type->{mapped}")
            normalized["type"] = mapped
    normalized.pop("action_type", None)
    normalized = _copy_known_aliases(normalized, trace)

    if normalized.get("type") == "skill":
        identity = normalized.pop("skill_identity", None)
        if isinstance(identity, dict):
            if "skill_id" not in normalized and (identity.get("skill_id") or identity.get("id")):
                normalized["skill_id"] = identity.get("skill_id") or identity.get("id")
                trace.mark("skill_identity->skill_id")
            if "skill_name" not in normalized and (identity.get("skill_name") or identity.get("name")):
                normalized["skill_name"] = identity.get("skill_name") or identity.get("name")
                trace.mark("skill_identity->skill_name")
        evidence = normalized.get("supporting_evidence")
        if isinstance(evidence, str):
            normalized["supporting_evidence"] = [evidence]
            trace.mark("supporting_evidence:string->list")
        elif evidence is None:
            normalized["supporting_evidence"] = []
            trace.mark("supporting_evidence:default", degraded=True)

    if normalized.get("type") == "tool":
        if not isinstance(normalized.get("reason"), str) or not normalized["reason"].strip():
            normalized["reason"] = "Continue the authorized investigation"
            trace.mark("reason:default", degraded=True)
        if not isinstance(normalized.get("hypothesis"), (str, dict)) or not normalized["hypothesis"]:
            normalized["hypothesis"] = "Initial investigation hypothesis"
            trace.mark("hypothesis:default", degraded=True)

    return normalized, trace


def validate_action(raw_action: dict[str, Any]) -> tuple[AgentAction, dict[str, Any]]:
    normalized, trace = normalize_action_payload(raw_action)
    try:
        action = ACTION_ADAPTER.validate_python(normalized)
    except ValidationError:
        trace.validation_result = "failed"
        raise
    trace.validation_result = "passed"
    return action, {
        "original_shape": trace.original_shape,
        "normalized_fields": trace.normalized_fields,
        "normalization_count": trace.normalization_count,
        "validation_result": trace.validation_result,
        "normalization_quality": trace.normalization_quality,
        "normalized_payload_hash": json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str),
    }

