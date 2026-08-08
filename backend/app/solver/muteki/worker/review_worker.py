from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..graph import MutekiGraph


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
        repeated: list[int] = []
        counts = Counter(item.description for item in intents)
        for description, count in counts.items():
            if count < 2 or description in existing_dead_ends:
                continue
            if any(item.description == description and item.status in {"open", "claimed"} for item in intents):
                continue
            dead_end_id = self.graph.add_dead_end(actor=self.worker_id, description=f"repeated route without progress: {description}")
            repeated.append(dead_end_id)
            notes.append(f"suppressed repeated route: {description}")
        branches: list[str] = []
        for fact in facts:
            text = fact.content
            if " or " not in text.casefold():
                continue
            alternatives = [item.strip() for item in text.split(" or ") if item.strip()]
            for alternative in alternatives[:3]:
                intent_id = self.graph.propose_intent(
                    actor=self.worker_id,
                    description=f"review branch: validate {alternative[:180]}",
                    payload={"worker_class": "explore", "review_branch": True, "source_fact_id": fact.fact_id},
                )
                branches.append(intent_id)
        return ReviewResult(tuple(suspicious), tuple(repeated), tuple(branches), tuple(notes))


__all__ = ["ReviewResult", "ReviewWorker"]
