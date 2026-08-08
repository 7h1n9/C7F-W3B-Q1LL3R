from __future__ import annotations

import json
from typing import Any

from .blackboard import BlackboardState
from .reducers.base import KnowledgeUpdate


def _append_unique(items: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {json.dumps(item, sort_keys=True, default=str) for item in items}
    result = list(items)
    for item in additions:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            result.append(dict(item))
            seen.add(key)
    return result


class KnowledgeStore:
    """Project reducer output into current Blackboard cognition."""

    def apply(self, state: BlackboardState, update: KnowledgeUpdate) -> BlackboardState:
        knowledge = dict(state.knowledge)
        # Phase 1.4 temporarily stored observations for loop tests.  Remove
        # that raw channel when creating the Phase 1.5 cognition projection.
        for raw_key in ("observations", "raw_result", "last_observation"):
            knowledge.pop(raw_key, None)

        verified_facts = list(knowledge.get("verified_facts") or [])
        verified_facts.extend(
            item for item in (knowledge.get("facts") or []) if item.get("verified") is True
        )
        verified_facts = _append_unique(verified_facts, update.verified_facts)
        hypotheses = list(knowledge.get("hypotheses") or [])
        hypotheses = _append_unique(hypotheses, update.hypotheses)
        findings = list(knowledge.get("findings") or [])
        findings = _append_unique(findings, update.findings)
        knowledge["vulnerabilities"] = list(knowledge.get("vulnerabilities") or [])
        knowledge["verified_facts"] = verified_facts
        knowledge["hypotheses"] = hypotheses
        knowledge["findings"] = findings
        knowledge.pop("facts", None)

        control = {**state.control, **update.control_updates}
        if update.control_updates.get("metadata_failure_increment"):
            control["metadata_failures"] = int(state.control.get("metadata_failures") or 0) + int(
                update.control_updates["metadata_failure_increment"]
            )
        control.pop("metadata_failure_increment", None)
        if update.control_updates.get("script_retry_increment"):
            control["script_retry_count"] = int(state.control.get("script_retry_count") or 0) + int(
                update.control_updates["script_retry_increment"]
            )
        control.pop("script_retry_increment", None)
        if control.get("script_retry_pending") and int(control.get("script_retry_count") or 0) >= 2:
            control["script_retry_pending"] = False
            control["automation_terminal"] = True
        tested_parameter = update.control_updates.get("tested_parameter")
        if tested_parameter:
            tested = [str(item) for item in (state.control.get("tested_parameters") or [])]
            if str(tested_parameter) not in tested:
                tested.append(str(tested_parameter))
            control["tested_parameters"] = tested
        return state.model_copy(
            update={
                "phase": update.next_phase or state.phase,
                "knowledge": knowledge,
                "control": control,
            },
            deep=True,
        )
