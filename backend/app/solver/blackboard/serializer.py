from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .models import BlackboardState

CURRENT_SCHEMA_VERSION = 1


def serialize_state(state: BlackboardState) -> dict[str, Any]:
    """Serialize state to JSON-compatible data with an explicit schema tag."""
    payload = state.model_dump(mode="json")
    payload["schema_version"] = CURRENT_SCHEMA_VERSION
    return payload


def deserialize_state(payload: Mapping[str, Any]) -> BlackboardState:
    """Load current state and migrate the Phase 1.1 flat shape in memory.

    The migration is intentionally a pure serializer concern.  It does not
    read legacy database rows or connect this adapter to a real Run.
    """
    data = deepcopy(dict(payload))
    data["schema_version"] = CURRENT_SCHEMA_VERSION

    knowledge = dict(data.get("knowledge") or {})
    if "facts" in data and "facts" not in knowledge:
        knowledge["facts"] = data["facts"]
    if "hypotheses" in data and "hypotheses" not in knowledge:
        knowledge["hypotheses"] = data["hypotheses"]
    data["knowledge"] = knowledge
    if "vulnerability_hypotheses" not in data:
        data["vulnerability_hypotheses"] = list(
            knowledge.get("vulnerability_hypotheses") or []
        )

    control = dict(data.get("control") or {})
    if "allowed_actions" in data and "allowed_actions" not in control:
        control["allowed_actions"] = data["allowed_actions"]
    data["control"] = control

    data.setdefault("goal", "")
    data.setdefault("history", [])
    data.setdefault("evidence_refs", [])
    data.setdefault("vulnerability_hypotheses", [])
    data.setdefault("version", 0)
    return BlackboardState.model_validate(data)
