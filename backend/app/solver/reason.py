from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .shared_graph.classification import ChallengeClassification

CLASSIFICATION_INTENT = "CLASSIFY_CHALLENGE: perform bounded reconnaissance to determine vulnerability type from response signatures"

TOOL_DOMAINS: dict[ChallengeClassification, frozenset[str]] = {
    ChallengeClassification.SQLI: frozenset({"sql_boolean_compare", "sqlmap_detect", "sqlmap_run"}),
    ChallengeClassification.PATH_TRAVERSAL: frozenset({"http_request", "file_read", "http_extract"}),
    ChallengeClassification.COMMAND_INJECTION: frozenset({"http_request", "http_extract"}),
    ChallengeClassification.SSRF: frozenset({"http_request"}),
    ChallengeClassification.IDOR: frozenset({"http_session_request", "http_extract"}),
    ChallengeClassification.JWT: frozenset({"jwt_inspect", "http_session_request"}),
    ChallengeClassification.SSTI: frozenset({"http_request", "http_extract"}),
    ChallengeClassification.XXE: frozenset({"http_request", "http_extract"}),
    ChallengeClassification.FILE_UPLOAD: frozenset({"http_request", "file_type"}),
    ChallengeClassification.GENERIC_WEB: frozenset({"http_request", "http_extract", "file_read"}),
}


@dataclass(frozen=True, slots=True)
class ReasonIntent:
    """A bounded intent with an explicit tool-domain identity."""

    description: str
    tool_name: str | None = None
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)


PlannerProvider = Callable[
    [Mapping[str, Any]],
    Sequence[str | Mapping[str, Any]] | Awaitable[Sequence[str | Mapping[str, Any]]],
]


class ReasonPlanner:
    """Plan only inside a classification-controlled tool domain.

    Classification is a hard gate, not a provider suggestion.  Provider
    output is normalized and filtered after the gate; an unclassified board
    can produce exactly one classifier intent and no exploit intent.
    """

    def __init__(self, provider: PlannerProvider | None = None, *, max_intents: int = 4) -> None:
        self.provider = provider
        self.max_intents = max(1, min(max_intents, 4))

    async def plan(self, snapshot: Mapping[str, Any]) -> list[ReasonIntent]:
        classification = self._classification(snapshot)
        if classification is None or not classification[1] >= 70:
            return [
                ReasonIntent(
                    CLASSIFICATION_INTENT,
                    tool_name="CLASSIFY_CHALLENGE",
                    allowed_tools=("http_request", "http_extract", "file_read"),
                )
            ]

        value, _confidence = classification
        allowed = TOOL_DOMAINS[value]
        if self.provider is not None:
            proposed = self.provider(snapshot)
            if inspect.isawaitable(proposed):
                proposed = await proposed
            intents = self._normalize(proposed, allowed)
        else:
            intents = self._fallback(value, allowed)

        deadends = {
            str(item.get("description", "")).casefold()
            for item in snapshot.get("deadends", [])
            if isinstance(item, Mapping)
        }
        return [
            item
            for item in intents
            if not any(deadend and deadend in item.description.casefold() for deadend in deadends)
        ][: self.max_intents]

    def _classification(self, snapshot: Mapping[str, Any]) -> tuple[ChallengeClassification, int] | None:
        value = snapshot.get("challenge_classification")
        if not isinstance(value, Mapping):
            for fact in snapshot.get("facts", []):
                if isinstance(fact, Mapping) and fact.get("fact_type") == "CHALLENGE_CLASSIFICATION":
                    value = fact.get("value")
                    break
        if not isinstance(value, Mapping):
            return None
        try:
            return ChallengeClassification(str(value["classification"]).upper()), int(value["confidence"])
        except (KeyError, TypeError, ValueError):
            return None

    def _normalize(
        self,
        values: Sequence[str | Mapping[str, Any]],
        allowed: frozenset[str],
    ) -> list[ReasonIntent]:
        result: list[ReasonIntent] = []
        for value in values:
            if isinstance(value, Mapping):
                description = value.get("description") or value.get("intent")
                tool_name = value.get("tool") or value.get("tool_name") or value.get("action")
            else:
                description = value
                tool_name = None
            if not isinstance(description, str) or not description.strip():
                continue
            tool = self._infer_tool(description, tool_name, allowed)
            if tool is not None:
                result.append(ReasonIntent(description.strip(), tool_name=tool, allowed_tools=tuple(sorted(allowed))))
        return result

    @staticmethod
    def _infer_tool(description: str, tool_name: Any, allowed: frozenset[str]) -> str | None:
        if isinstance(tool_name, str) and tool_name in allowed:
            return tool_name
        first = description.strip().split(":", 1)[0].strip()
        if first in allowed:
            return first
        for tool in sorted(allowed, key=len, reverse=True):
            if tool in description:
                return tool
        return None

    @staticmethod
    def _fallback(value: ChallengeClassification, allowed: frozenset[str]) -> list[ReasonIntent]:
        preferred_tools = {
            ChallengeClassification.SQLI: "sql_boolean_compare",
            ChallengeClassification.PATH_TRAVERSAL: "http_request",
            ChallengeClassification.COMMAND_INJECTION: "http_request",
            ChallengeClassification.SSRF: "http_request",
            ChallengeClassification.IDOR: "http_session_request",
            ChallengeClassification.JWT: "jwt_inspect",
            ChallengeClassification.SSTI: "http_request",
            ChallengeClassification.XXE: "http_request",
            ChallengeClassification.FILE_UPLOAD: "http_request",
            ChallengeClassification.GENERIC_WEB: "http_request",
        }
        tool = preferred_tools.get(value)
        if tool not in allowed:
            tool = next(iter(sorted(allowed)))
        payload_hint = {
            ChallengeClassification.PATH_TRAVERSAL: " with a bounded ../ payload",
            ChallengeClassification.COMMAND_INJECTION: " with a bounded ;, |, or && payload",
            ChallengeClassification.SSRF: " with a bounded private-address URL payload",
            ChallengeClassification.SSTI: " with a bounded {{7*7}} payload",
            ChallengeClassification.XXE: " with a bounded <!DOCTYPE SYSTEM payload",
            ChallengeClassification.FILE_UPLOAD: " using multipart/form-data",
        }.get(value, "")
        return [
            ReasonIntent(
                f"{tool}: perform the next bounded {value.value} validation step{payload_hint}",
                tool_name=tool,
                allowed_tools=tuple(sorted(allowed)),
            )
        ]


__all__ = ["CLASSIFICATION_INTENT", "ReasonIntent", "ReasonPlanner", "TOOL_DOMAINS"]
