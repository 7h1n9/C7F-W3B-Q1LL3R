from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .graph import MutekiGraph
from .outcomes import stable_branch_id, stable_route_hash
from .recon.fingerprint import ClassificationResult, classify_challenge

SQL_TOOLS = frozenset({
    "sql_boolean_compare",
    "oracle_expression_calibration",
    "mysql_metadata_discovery",
    "boolean_config_extract",
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
    # Session requests are the bounded authentication bootstrap/continuation
    # path for a classified web surface.  They do not grant any SQL or shell
    # capability; keeping them in the web domains lets a public demo account
    # establish the session required before the domain-specific probe.
    "PATH_TRAVERSAL": frozenset({"http_request", "http_session_request", "file_read", "http_extract"}),
    "COMMAND_INJECTION": frozenset({"http_request", "http_session_request", "http_extract"}),
    "SSRF": frozenset({"http_request", "http_session_request"}),
    "JWT": frozenset({"jwt_inspect", "http_session_request"}),
    "SSTI": frozenset({"http_request", "http_session_request", "http_extract"}),
    "XXE": frozenset({"http_request", "http_session_request", "http_extract"}),
    "FILE_UPLOAD": frozenset({"http_request", "http_session_request", "file_type"}),
    "GENERIC_WEB": frozenset({"http_request", "http_session_request", "http_extract", "file_read", "file_search"}),
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


ReasonProvider = Callable[
    [Mapping[str, Any]],
    Sequence[Mapping[str, Any] | str] | str | Awaitable[Sequence[Mapping[str, Any] | str] | str],
]


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
        known_goals = {item["description"].casefold() for item in snapshot["intents"]}
        dead_ends = {item["description"].casefold() for item in snapshot["dead_ends"]}
        classification = classify_challenge(self.metadata, snapshot.get("facts", ()))
        if classification is not None and classification.confidence < 70:
            classification = None
        provider_goal_met = False
        provider_verdict = "explore"
        provider_drift = ""
        if self.provider is None:
            raw: Sequence[Mapping[str, Any] | str] = ()
        else:
            # The official Muteki Reason prompt consumes the full labelled
            # SharedGraph view, not a short JSON tail. Keep the generic
            # snapshot contract for compatibility, but attach the canonical
            # summary when the selected graph exposes it.
            provider_snapshot = dict(snapshot)
            summary = getattr(graph, "to_reason_summary", None)
            if callable(summary):
                try:
                    provider_snapshot["_reason_summary"] = summary()
                except Exception:
                    pass
            pin_context = getattr(graph, "fact_pin_context", None)
            if callable(pin_context):
                try:
                    provider_snapshot["_fact_pin_context"] = pin_context()
                except Exception:
                    pass
            raw_value = self.provider(provider_snapshot)
            raw_value = await raw_value if inspect.isawaitable(raw_value) else raw_value
            if isinstance(raw_value, str):
                from .adapter.upstream_reason import parse_upstream_reason_reply

                raw = parse_upstream_reason_reply(raw_value, max_intents=self.max_intents)
            else:
                raw = raw_value
            # The official adapter returns a list-compatible envelope so the
            # existing provider contract remains intact.  Read the envelope
            # after both string parsing and direct provider injection; this
            # keeps the upstream verdict from being lost at either boundary.
            provider_goal_met = bool(getattr(raw, "goal_met", False))
            provider_verdict = str(getattr(raw, "verdict", "explore") or "explore")
            provider_drift = str(getattr(raw, "drift", "") or "")
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
            # Intents are immutable graph decisions.  Once a route has been
            # concluded, proposing the exact same description again is a
            # replay loop, not a new OODA branch.  Strategy providers should
            # express a retry or alternate route with a distinct goal.
            if normalized in known_goals or any(dead and dead in normalized for dead in dead_ends):
                continue
            normalized_payload = dict(payload) if isinstance(payload, Mapping) else {}
            tool_name = _tool_name(normalized_payload, normalized)
            if _blocked_before_classification(tool_name, normalized, classification):
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
            # A provider may intentionally return no next action after an
            # evidence-backed route is exhausted (for example, every
            # declared SQL field was tested without a confirmed oracle).
            # Do not resurrect the generic SQL fallback in that case: an
            # invented action would violate the Blackboard-driven stop rule.
            # Canonical Muteki's Reason keeps an empty model result empty: when
            # the SharedGraph says every direction is concluded, it does not
            # resurrect a generic fallback intent.  Preserve the old fallback
            # only for the provider-less compatibility mode used by isolated
            # graph tests; a real provider returning no next action is an
            # explicit exhausted route.
            if provider_verdict in {"course_correct", "complete"}:
                return ReasonResult(
                    provider_goal_met,
                    (),
                    verdict=provider_verdict,
                    drift=provider_drift,
                )
            if (
                classification is not None
                and classification.classification == "SQLI"
                and (raw or self.provider is not None)
            ):
                return ReasonResult(
                    provider_goal_met,
                    (),
                    verdict=provider_verdict,
                    drift=provider_drift,
                )
            fallback = _fallback_intent(classification, snapshot.get("pocs", ()))
            if fallback.goal.casefold() in known_goals:
                return ReasonResult(
                    provider_goal_met,
                    (),
                    verdict=provider_verdict,
                    drift=provider_drift,
                )
            proposals.append(fallback)
        return ReasonResult(
            provider_goal_met,
            tuple(proposals),
            verdict=provider_verdict,
            drift=provider_drift,
        )

    def write_intents(self, graph: MutekiGraph, result: ReasonResult, *, actor: str = "coordinator") -> list[str]:
        intent_ids: list[str] = []
        for item in result.intents:
            payload = {
                "worker_class": item.worker_class,
                "rationale": item.rationale,
                **item.payload,
            }
            route_hash = stable_route_hash(payload, goal=item.goal)
            payload["route_hash"] = route_hash
            payload["branch_id"] = stable_branch_id(
                route_hash,
                str(payload.get("branch_id") or ""),
            )
            # The engine attempt is assigned only when a Coordinator claims
            # the Intent.  Keeping the field in the Intent envelope makes the
            # identity explicit without coupling Reason to an engine.
            payload.setdefault("engine_attempt_id", "")
            intent_ids.append(
                graph.propose_intent(
                    actor=actor,
                    description=item.goal,
                    payload=payload,
                )
            )
        return intent_ids


def _tool_name(payload: Mapping[str, Any], normalized_goal: str) -> str | None:
    value = payload.get("tool_name") or payload.get("tool") or payload.get("action")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for tool in SQL_TOOLS:
        if tool in normalized_goal:
            return tool
    return None


def _blocked_before_classification(
    tool_name: str | None,
    normalized_goal: str,
    classification: ClassificationResult | None,
) -> bool:
    """Reject SQL actions only while the classification gate is unresolved."""

    if classification is not None:
        return False
    if tool_name in SQL_TOOLS:
        return True
    return any(tool in normalized_goal for tool in SQL_TOOLS)


def _fallback_intent(classification: ClassificationResult | None, pocs: Sequence[Mapping[str, Any]] = ()) -> IntentProposal:
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
    if pocs:
        poc_id = str(pocs[-1].get("id") or pocs[-1].get("poc_id") or "")
        return IntentProposal(
            "EXPLOIT_WITH_POC",
            worker_class="exploit",
            rationale="Reuse a graph-backed PoC only after the challenge has a high-confidence classification.",
            payload={"poc_id": poc_id, "classification": classification.classification},
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
