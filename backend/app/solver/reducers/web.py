from __future__ import annotations

from typing import Any

from ..observation import SolverObservation
from .base import KnowledgeUpdate


def _result_views(raw_result: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    structured = raw_result.get("structured_result")
    if isinstance(structured, dict):
        return raw_result, structured
    return (raw_result,)


def _status_code(raw_result: dict[str, Any]) -> int | None:
    for view in _result_views(raw_result):
        for key in ("status_code", "status", "http_status"):
            value = view.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                continue
    return None


def _boolean_value(raw_result: dict[str, Any], *keys: str) -> bool | None:
    for view in _result_views(raw_result):
        for key in keys:
            if key not in view:
                continue
            value = view[key]
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                return value.strip().lower() == "true"
    return None


class WebObservationReducer:
    """Reduce HTTP and Boolean observations to verified Solver knowledge."""

    def reduce(self, observation: SolverObservation) -> KnowledgeUpdate:
        if observation.action_name == "http_request":
            return self._reduce_http(observation)
        if observation.action_name == "sql_boolean_compare":
            return self._reduce_boolean(observation)
        return KnowledgeUpdate(
            verified_facts=list(observation.facts),
            control_updates={"last_action": observation.action_name},
        )

    @staticmethod
    def _reduce_http(observation: SolverObservation) -> KnowledgeUpdate:
        status = _status_code(observation.raw_result)
        if not observation.success:
            return KnowledgeUpdate(
                hypotheses=[
                    {
                        "type": "HTTP_BASELINE_INCONCLUSIVE",
                        "action": observation.action_name,
                        "reason": "worker did not report success",
                    }
                ],
                control_updates={"baseline_status": "INCONCLUSIVE"},
            )
        facts = [
            {"type": "HTTP_RESPONSE", "status": status, "verified": True},
            {"type": "HTTP_ENDPOINT_FOUND", "status": status, "verified": True},
        ]
        return KnowledgeUpdate(
            verified_facts=facts,
            next_phase="VALIDATION",
            control_updates={"baseline_status": "BASELINE_CONFIRMED"},
        )

    @staticmethod
    def _reduce_boolean(observation: SolverObservation) -> KnowledgeUpdate:
        true_value = _boolean_value(
            observation.raw_result,
            "true",
            "true_result",
            "true_signature",
            "stable_true",
        )
        false_value = _boolean_value(
            observation.raw_result,
            "false",
            "false_result",
            "false_signature",
            "stable_false",
        )
        oracle_fact = {
            "type": "BOOLEAN_ORACLE",
            "true": true_value,
            "false": false_value,
            "verified": False,
        }
        if observation.success and true_value is True and false_value is False:
            oracle_fact["verified"] = True
            return KnowledgeUpdate(
                verified_facts=[
                    oracle_fact,
                    {"type": "VALIDATION_SUCCESS", "verified": True},
                ],
                next_phase="EXPLOITATION",
                control_updates={
                    "validation_status": "VALIDATION_SUCCESS",
                    "strategy_needed": False,
                },
            )
        return KnowledgeUpdate(
            hypotheses=[
                {
                    "type": "VALIDATION_INCONCLUSIVE",
                    "true": true_value,
                    "false": false_value,
                    "strategy_needed": True,
                }
            ],
            next_phase="VALIDATION",
            control_updates={
                "validation_status": "VALIDATION_INCONCLUSIVE",
                "strategy_needed": True,
            },
        )
