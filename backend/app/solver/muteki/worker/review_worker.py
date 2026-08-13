from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha1

from ..graph import MutekiGraph

_REVIEW_TOOL_NAMES = frozenset(
    {
        "file_read",
        "http_extract",
        "http_request",
        "http_session_request",
        "jwt_inspect",
        "mysql_metadata_discovery",
        "oracle_expression_calibration",
        "sql_boolean_compare",
    }
)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    suspicious_fact_ids: tuple[int, ...] = ()
    dead_end_ids: tuple[int, ...] = ()
    branch_intent_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class ReviewWorker:
    """Audit graph evidence and suppress repetition without solving the task."""

    def __init__(self, graph: MutekiGraph, *, worker_id: str = "review-worker") -> None:
        self.graph = graph
        self.worker_id = worker_id

    def run(self) -> ReviewResult:
        facts = self.graph.facts()
        intents = self.graph.intents()
        existing_dead_ends = {item.description for item in self.graph.dead_ends()}
        existing_goals = {item.description.casefold() for item in intents}
        suspicious: list[int] = []
        notes: list[str] = []
        for fact in facts:
            if fact.verified and fact.evidence_refs:
                continue
            suspicious.append(fact.fact_id)
            self.graph.add_fact(
                actor=self.worker_id,
                content=f"REVIEW_NEEDED fact_id={fact.fact_id}; reason=unverified_or_missing_evidence",
                verified=False,
                evidence_refs=fact.evidence_refs,
                dedupe_key=f"review:fact:{fact.fact_id}",
            )
            if hasattr(self.graph, "add_review_finding"):
                self.graph.add_review_finding(
                    actor=self.worker_id,
                    kind="unverified_fact",
                    severity="warn",
                    summary=f"Fact {fact.fact_id} lacks verified evidence",
                    evidence_seqs=list(fact.evidence_refs),
                )
        repeated: list[int] = []
        counts = Counter(item.description for item in intents)
        for description, count in counts.items():
            if count < 2 or description in existing_dead_ends:
                continue
            if any(item.description == description and item.status in {"open", "claimed"} for item in intents):
                continue
            matching = [item for item in intents if item.description == description]
            route_hash = next(
                (str(item.payload.get("route_hash") or "") for item in matching if item.payload),
                "",
            )
            if route_hash and hasattr(self.graph, "add_review_proposal"):
                # Official Muteki keeps tier-2 control-plane changes in the
                # review queue.  The Coordinator applies the proposal after
                # checking genuine failures and confidence; Review itself
                # must not mutate route state on the upstream graph.
                self.graph.add_review_proposal(
                    actor=self.worker_id,
                    marker="ROUTE_SUPPRESS",
                    tier="tier2",
                    payload={
                        "route_hash": route_hash,
                        "label": description[:160],
                        "reason": "repeated route without progress",
                        "until": "new_evidence",
                        "matching_intents": [item.intent_id for item in matching],
                        "confidence": 1.0,
                    },
                )
                notes.append(f"proposed route suppression {route_hash}: {description}")
            elif route_hash and hasattr(self.graph, "suppress_route"):
                # Compatibility Graph has no proposal queue; preserve its
                # existing direct suppression behavior until that backend is
                # retired.
                self.graph.suppress_route(
                    actor=self.worker_id,
                    route_hash=route_hash,
                    label=description[:160],
                    reason="repeated route without progress",
                )
                notes.append(f"suppressed route {route_hash}: {description}")
            else:
                dead_end_id = self.graph.add_dead_end(
                    actor=self.worker_id,
                    description=f"repeated route without progress: {description}",
                )
                repeated.append(dead_end_id)
                notes.append(f"suppressed route: {description}")
            if hasattr(self.graph, "add_review_finding"):
                self.graph.add_review_finding(
                    actor=self.worker_id,
                    kind="repeated_route",
                    severity="warn",
                    summary=f"Repeated route without progress: {description[:400]}",
                    intent_ids=[item.intent_id for item in matching],
                    route_hash=route_hash,
                    recommended_actions=["suppress_route", "choose_alternate_branch"],
                )
        branches: list[str] = []
        for fact in facts:
            text = fact.content
            if " or " not in text.casefold():
                continue
            alternatives = [item.strip() for item in text.split(" or ") if item.strip()]
            branch_id = ""
            branch_specs = []
            for alternative in alternatives[:3]:
                child_id = f"review-{sha1(alternative.encode('utf-8', 'ignore')).hexdigest()[:10]}"
                branch_specs.append({
                    "id": child_id,
                    "assumption": alternative[:180],
                    "prove_or_disprove": f"validate {alternative[:180]}",
                })
            if branch_specs and hasattr(self.graph, "split_branch"):
                result = self.graph.split_branch(
                    actor=self.worker_id,
                    title=f"Review alternatives from fact {fact.fact_id}",
                    branches=branch_specs,
                )
                branch_id = str(result.get("branch_id") or "")
            for alternative in alternatives[:3]:
                goal = f"review branch: validate {alternative[:180]}"
                if goal.casefold() in existing_goals:
                    continue
                child_id = f"review-{sha1(alternative.encode('utf-8', 'ignore')).hexdigest()[:10]}"
                payload = {
                    "worker_class": "explore",
                    "review_branch": True,
                    "source_fact_id": fact.fact_id,
                    "branch_id": child_id if branch_id else "",
                }
                tool_name = _explicit_review_tool(alternative)
                if tool_name:
                    payload["tool_name"] = tool_name
                    payload["tool_identity_source"] = "review_allowlist"
                intent_id = self.graph.propose_intent(
                    actor=self.worker_id,
                    description=goal,
                    payload=payload,
                )
                branches.append(intent_id)
                existing_goals.add(goal.casefold())
        return ReviewResult(tuple(suspicious), tuple(repeated), tuple(branches), tuple(notes))


def _explicit_review_tool(text: str) -> str | None:
    """Return a tool only when the review text names one allowlisted tool exactly."""

    candidates = {
        match.casefold()
        for match in re.findall(r"[a-z][a-z0-9_]{2,63}", str(text or ""))
    }
    for name in sorted(_REVIEW_TOOL_NAMES):
        if name.casefold() in candidates:
            return name
    return None


__all__ = ["ReviewResult", "ReviewWorker"]
