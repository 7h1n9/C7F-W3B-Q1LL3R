from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .graph import MutekiGraph
from .recon.fingerprint import ClassificationResult, classify_challenge

SQL_TOOLS = frozenset({
    "sql_boolean_compare",
    "sql_injection_probe",
    "sql_union_probe",
    "sqlmap_detect",
    "sqlmap_run",
    "oracle_probe_matrix",
    "boolean_config_extract",
})

TOOL_DOMAINS = {
    "SQLI": SQL_TOOLS,
    "IDOR": frozenset({"http_session_request", "http_extract", "http_request"}),
    "PATH_TRAVERSAL": frozenset({"http_request", "file_read", "http_extract"}),
    "COMMAND_INJECTION": frozenset({"http_request", "http_extract"}),
    "SSRF": frozenset({"http_request"}),
    "JWT": frozenset({"jwt_inspect", "http_session_request"}),
    "SSTI": frozenset({"http_request", "http_extract"}),
    "XXE": frozenset({"http_request", "http_extract"}),
    "FILE_UPLOAD": frozenset({"http_request", "file_type"}),
    "GENERIC_WEB": frozenset({"http_request", "http_extract", "file_read", "file_search"}),
}

TOOL_PREFERENCE = {
    "IDOR": "http_session_request",
    "PATH_TRAVERSAL": "http_request",
    "COMMAND_INJECTION": "http_request",
    "SSRF": "http_request",
    "JWT": "jwt_inspect",
    "SSTI": "http_request",
    "XXE": "http_request",
    "FILE_UPLOAD": "http_request",
    "GENERIC_WEB": "http_request",
}


@dataclass(frozen=True, slots=True)
class IntentProposal:
    goal: str
    worker_class: str = "code"
    rationale: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReasonResult:
    goal_met: bool
    intents: tuple[IntentProposal, ...]
    verdict: str = "explore"
    drift: str = ""


ReasonProvider = Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any] | str] | Awaitable[Sequence[Mapping[str, Any] | str]]]


class MutekiReason:
    """Cheap, graph-only planner that produces bounded claimable intents."""

    def __init__(self, provider: ReasonProvider | None = None, *, max_intents: int = 4, metadata: Mapping[str, Any] | None = None) -> None:
        self.provider = provider
        self.max_intents = max(1, min(max_intents, 4))
        self.metadata = dict(metadata or {})

    async def reason(self, graph: MutekiGraph) -> ReasonResult:
        snapshot = graph.snapshot()
        if snapshot["flags"]:
            return ReasonResult(True, (), verdict="complete")
        open_goals = {
            item["description"].casefold()
            for item in snapshot["intents"]
            if item["status"] in {"open", "claimed"}
        }
        known_goals = {item["description"].casefold() for item in snapshot["intents"]}
        dead_ends = {item["description"].casefold() for item in snapshot["dead_ends"]}
        classification = classify_challenge(self.metadata, snapshot.get("facts", ()))
        if classification is not None and classification.confidence < 70:
            classification = None
        if self.provider is None:
            raw: Sequence[Mapping[str, Any] | str] = ()
        else:
            raw_value = self.provider(snapshot)
            raw = await raw_value if inspect.isawaitable(raw_value) else raw_value
        proposals: list[IntentProposal] = []
        for item in raw:
            if isinstance(item, Mapping):
                goal = item.get("goal") or item.get("description")
                worker_class = item.get("worker_class", "code")
                rationale = item.get("rationale", "")
                payload = item.get("payload", {})
            else:
                goal, worker_class, rationale, payload = item, "code", "", {}
            if not isinstance(goal, str) or not goal.strip():
                continue
            normalized = goal.strip().casefold()
            if normalized in open_goals or any(dead and dead in normalized for dead in dead_ends):
                continue
            normalized_payload = dict(payload) if isinstance(payload, Mapping) else {}
            tool_name = _tool_name(normalized_payload, normalized)
            if _blocked_before_classification(tool_name, normalized):
                continue
            if classification and classification.classification != "SQLI":
                allowed = TOOL_DOMAINS.get(classification.classification, TOOL_DOMAINS["GENERIC_WEB"])
                if tool_name and tool_name not in allowed:
                    continue
                if tool_name is None:
                    normalized_payload.setdefault("tool_name", next(iter(allowed)))
            elif classification and classification.classification == "SQLI":
                if tool_name and tool_name not in SQL_TOOLS:
                    continue
            proposals.append(IntentProposal(goal.strip(), str(worker_class), str(rationale), normalized_payload))
            if len(proposals) >= self.max_intents:
                break
        if not proposals:
            fallback = _fallback_intent(classification)
            if fallback.goal.casefold() in known_goals:
                return ReasonResult(False, ())
            proposals.append(fallback)
        return ReasonResult(False, tuple(proposals))

    def write_intents(self, graph: MutekiGraph, result: ReasonResult, *, actor: str = "coordinator") -> list[str]:
        return [
            graph.propose_intent(actor=actor, description=item.goal, payload={"worker_class": item.worker_class, "rationale": item.rationale, **item.payload})
            for item in result.intents
        ]


def _tool_name(payload: Mapping[str, Any], normalized_goal: str) -> str | None:
    value = payload.get("tool_name") or payload.get("tool") or payload.get("action")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for tool in SQL_TOOLS:
        if tool in normalized_goal:
            return tool
    return None


def _blocked_before_classification(tool_name: str | None, normalized_goal: str) -> bool:
    if tool_name in SQL_TOOLS:
        return True
    return any(tool in normalized_goal for tool in SQL_TOOLS)


def _fallback_intent(classification: ClassificationResult | None) -> IntentProposal:
    if classification is None:
        return IntentProposal(
            "CLASSIFY_CHALLENGE",
            worker_class="recon",
            rationale="Challenge classification is not established; only bounded reconnaissance is allowed.",
            payload={"tool_name": "http_request", "classification_gate": "required"},
        )
    if classification.classification == "SQLI":
        return IntentProposal(
            "sql_boolean_compare",
            worker_class="exploit",
            rationale="SQLI is explicitly or evidentially classified.",
            payload={"tool_name": "sql_boolean_compare"},
        )
    allowed = TOOL_DOMAINS.get(classification.classification, TOOL_DOMAINS["GENERIC_WEB"])
    tool = TOOL_PREFERENCE.get(classification.classification) or sorted(allowed)[0]
    return IntentProposal(
        "EXPLORE_ENDPOINTS" if classification.classification == "GENERIC_WEB" else f"explore {classification.classification.lower()} surface",
        worker_class="recon",
        rationale=f"Only the {classification.classification} tool domain is enabled.",
        payload={"tool_name": tool, "classification": classification.classification},
    )


__all__ = ["IntentProposal", "MutekiReason", "ReasonResult", "SQL_TOOLS", "TOOL_DOMAINS"]
