from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha1
from typing import Any

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

# Markers that a fact is a guess rather than an observation.  Kept deliberately
# narrow: rejecting a real observation costs far more than verifying one extra
# candidate, so this list holds only explicit, subjective hedging.  Words that
# merely soften a technical statement ("may be", "likely", "assume") are NOT
# here -- in a pentest write-up they are usually a described hypothesis that is
# exactly what verification is for.
_SPECULATION_MARKERS = (
    "i think",
    "i guess",
    "not sure",
    "guess",
    "maybe",
    "perhaps",
    "possibly",
    "probably",
    "猜测",
    "不确定",
    "也许",
    "怀疑",
)

# One Review pass must not try to conclude the whole board: it is a
# prioritizer, not a sweeper.  Newest candidates first, because the round that
# just ran is the one the team is actually building on.
_MAX_PENDING_FACTS_PER_REVIEW = 12
# And it must not flood the Coordinator with verification work either: each
# dispatched unit is a real Worker turn.
_MAX_VERIFICATION_DISPATCH_PER_REVIEW = 6

_VERIFY_INTENT_PREFIX = "verify fact"
_REVIEW_MARKER = "REVIEW_NEEDED"
# The official SharedGraph's own verifier unit is named ``I-verify-<seq>-<hash>``.
# Recognising it matters twice: it is the idempotence anchor for a challenged
# candidate, and it is what the Coordinator uses to dispatch verification ahead
# of exploration.
_OFFICIAL_VERIFY_INTENT_PREFIX = "I-verify-"

_OFFICIAL_VERIFY_ID_RE = re.compile(rf"^{_OFFICIAL_VERIFY_INTENT_PREFIX}(\d+)-")
_VERIFY_GOAL_RE = re.compile(rf"^{re.escape(_VERIFY_INTENT_PREFIX)} #(\d+):")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def verification_unit_target(in_intent) -> int:
    """Return the fact sequence a verification unit targets, else 0.

    Three shapes are recognised because the platform and the official graph
    each name their units differently: the platform's own dispatch carries
    ``verification_target_seq``, and the official ``challenge_fact`` names its
    unit ``I-verify-<seq>-<hash>`` (its payload carries no target field).
    """

    payload = dict(getattr(in_intent, "payload", None) or {})
    target = int(payload.get("verification_target_seq") or 0)
    if target:
        return target
    match = _OFFICIAL_VERIFY_ID_RE.match(str(getattr(in_intent, "intent_id", "") or ""))
    if match:
        return int(match.group(1))
    match = _VERIFY_GOAL_RE.match(str(getattr(in_intent, "description", "") or ""))
    if match:
        return int(match.group(1))
    return 0


def is_verification_unit(in_intent) -> bool:
    """True when an Intent exists to settle an already-known candidate."""

    if verification_unit_target(in_intent):
        return True
    payload = dict(getattr(in_intent, "payload", None) or {})
    if payload.get("verification_target_seq") or payload.get("review_branch"):
        return True
    return str(getattr(in_intent, "description", "") or "").startswith(
        f"{_VERIFY_INTENT_PREFIX} alternative:"
    )


def _looks_like_speculation(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in _SPECULATION_MARKERS)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    suspicious_fact_ids: tuple[int, ...] = ()
    dead_end_ids: tuple[int, ...] = ()
    branch_intent_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Facts this pass gave a terminal review verdict (merged / rejected).
    concluded_fact_ids: tuple[int, ...] = ()


class ReviewWorker:
    """Audit graph evidence and suppress repetition without solving the task.

    The part that matters for a long Run is what it does with *candidate*
    facts.  Marking them "REVIEW_NEEDED" and moving on is what let "待验证"
    grow without bound: nothing on the platform was ever allowed to conclude
    one.  Following the upstream SharedGraph, every candidate now gets a real
    verdict:

    * already covered by a verified fact -> ``merge`` (it is a duplicate, not a
      question);
    * hedging with no evidence -> ``reject`` (it is a guess, and a guess that
      stays pending forever is noise);
    * everything else -> ``challenge`` plus a dispatched verification unit, so
      the candidate becomes *work* the swarm can actually finish.

    Nothing here fabricates verification.  Only the official witness gate
    (``VERIFIED_FACT=`` backed by real output) can promote a fact; this worker's
    job is to make sure every candidate is either proven, disproven, or queued
    for one of those two outcomes.
    """

    def __init__(self, graph: MutekiGraph, *, worker_id: str = "review-worker") -> None:
        self.graph = graph
        self.worker_id = worker_id

    def run(self) -> ReviewResult:
        facts = self.graph.facts()
        intents = self.graph.intents()
        existing_dead_ends = {item.description for item in self.graph.dead_ends()}
        existing_goals = {item.description.casefold() for item in intents}
        review_fact = getattr(self.graph, "review_fact", None)

        verified_text = {
            _normalize(fact.content)
            for fact in facts
            if fact.verified and fact.evidence_refs
        }
        verified_by_text: dict[str, int] = {}
        for fact in facts:
            if fact.verified and fact.evidence_refs:
                verified_by_text.setdefault(_normalize(fact.content), fact.fact_id)

        concluded: list[int] = []
        dispatched_facts: list[int] = []
        branch_intents: list[str] = []
        notes: list[str] = []

        concluded, dispatched_facts, branch_intents, notes = self._converge_candidates(
            facts=facts,
            verified_text=verified_text,
            verified_by_text=verified_by_text,
            review_fact=review_fact,
            existing_goals=existing_goals,
        )

        repeated: list[int] = []
        counts = Counter(item.description for item in intents)
        for description, count in counts.items():
            if count < 2 or description in existing_dead_ends:
                continue
            if any(
                item.description == description and item.status in {"open", "claimed"}
                for item in intents
            ):
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

        return ReviewResult(
            tuple(dispatched_facts),
            tuple(repeated),
            tuple(branch_intents),
            tuple(notes),
            tuple(concluded),
        )

    def _converge_candidates(
        self,
        *,
        facts: list[Any],
        verified_text: set[str],
        verified_by_text: dict[str, int],
        review_fact,
        existing_goals: set[str],
    ) -> tuple[list[int], list[int], list[str], list[str]]:
        """Give every pending candidate a verdict or a verification unit."""

        # Idempotence anchor.  The old implementation re-wrote a REVIEW_NEEDED
        # marker every pass, which is precisely what made the pending set grow;
        # the verification unit itself is now the marker, so a candidate that
        # already has one must be left alone.  Without this every Review pass
        # would queue the same work again and the "待验证" count would climb by
        # construction.
        already_queued = {
            target
            for target in (verification_unit_target(item) for item in self.graph.intents())
            if target
        }
        pending = [
            fact
            for fact in facts
            if not (fact.verified and fact.evidence_refs)
            and not str(fact.content).startswith(_REVIEW_MARKER)
            and int(fact.fact_id) not in already_queued
        ]
        # Newest first: the most recent round is the one worth converging.
        pending.sort(key=lambda fact: fact.fact_id, reverse=True)

        concluded: list[int] = []
        dispatched: list[int] = []
        branch_intents: list[str] = []
        notes: list[str] = []
        dispatched_count = 0

        for fact in pending[:_MAX_PENDING_FACTS_PER_REVIEW]:
            normalized = _normalize(fact.content)
            if not normalized:
                continue
            # 1. Already covered by a verified fact: it is a duplicate, and a
            #    duplicate has a decided answer.
            duplicate_of = verified_by_text.get(normalized)
            if duplicate_of is not None and callable(review_fact):
                self._apply_review(
                    review_fact,
                    fact.fact_id,
                    action="merge",
                    reason="duplicate of a verified fact",
                    to_fact_seq=duplicate_of,
                )
                concluded.append(fact.fact_id)
                notes.append(f"merged fact {fact.fact_id} into verified fact {duplicate_of}")
                continue
            # 2. A hedge with no evidence is a guess, not a question.
            if _looks_like_speculation(fact.content) and not fact.evidence_refs:
                if callable(review_fact):
                    self._apply_review(
                        review_fact,
                        fact.fact_id,
                        action="reject",
                        reason="speculation without evidence",
                    )
                concluded.append(fact.fact_id)
                notes.append(f"rejected speculative fact {fact.fact_id}")
                continue
            # 3. Everything else is verification work.
            if dispatched_count >= _MAX_VERIFICATION_DISPATCH_PER_REVIEW:
                break
            goal = self._verification_goal(fact)
            if goal.casefold() in existing_goals:
                continue
            if callable(review_fact):
                # Ask the graph to challenge it.  On the official backend this
                # *is* the dispatch: ``challenge_fact`` creates the
                # ``I-verify-<seq>-<hash>`` verifier unit itself, with the
                # verification goal as its rationale.  Challenging first (and
                # only filling the gap afterwards) is what keeps the platform
                # from queueing the same candidate twice.
                self._apply_review(
                    review_fact,
                    fact.fact_id,
                    action="challenge",
                    reason="candidate awaiting verification",
                    verification_goal=goal,
                )
            created: list[str] = []
            is_branch = False
            has_alternatives = fact.content.casefold().count(" or ") > 0
            if not self._has_verification_unit(fact.fact_id) or has_alternatives:
                # Two cases need the platform to supply units: a backend
                # without the official behaviour (compatibility graph), and an
                # "A or B" candidate.  The official verifier unit is a single
                # generic turn and does not split the alternatives, so the
                # platform adds one unit per branch -- otherwise half of the
                # candidate would never be tested.
                created, is_branch = self._dispatch_verification(fact, goal)
            dispatched.append(fact.fact_id)
            dispatched_count += 1
            existing_goals.add(goal.casefold())
            if is_branch:
                branch_intents.extend(created)
        return concluded, dispatched, branch_intents, notes

    def _has_verification_unit(self, fact_seq: int) -> bool:
        targets = {
            target
            for target in (verification_unit_target(item) for item in self.graph.intents())
            if target
        }
        return int(fact_seq) in targets

    def _verification_goal(self, fact) -> str:
        """Describe the shortest path that would settle this candidate."""

        detail = re.sub(r"\s+", " ", str(fact.content or "")).strip()[:180]
        return (
            f"{_VERIFY_INTENT_PREFIX} #{fact.fact_id}: confirm or refute "
            f"\"{detail}\" with real output from this target. "
            "Report VERIFIED_FACT=<finding + proof> if it holds, "
            "DEADEND=<reason> if it does not."
        )

    def _dispatch_verification(self, fact, goal: str) -> tuple[list[str], bool]:
        """Create the verification unit(s) for one candidate.

        Returns ``(intent_ids, is_branch)``.  An "A or B" candidate becomes one
        unit per alternative -- each is a separate thing to prove or disprove,
        and collapsing them would leave half the candidate unverified.
        """

        proposer = getattr(self.graph, "propose_intent", None)
        if not callable(proposer):
            return [], False
        payload: dict[str, Any] = {
            "worker_class": "explore",
            # The Coordinator uses this to dispatch verification ahead of new
            # exploration: proving a candidate the team already produced is
            # cheaper than walking a fresh direction, and it is the only way a
            # candidate ever leaves the pending set.
            "verification_target_seq": int(fact.fact_id),
            "review_approved": True,
            "route_hash": f"verify:{fact.fact_id}",
        }
        tool_name = _explicit_review_tool(fact.content)
        if tool_name:
            payload["tool_name"] = tool_name
            payload["tool_identity_source"] = "review_allowlist"

        alternatives = (
            [part.strip() for part in fact.content.split(" or ") if part.strip()][:3]
            if fact.content.casefold().count(" or ") > 0
            else []
        )
        if not alternatives:
            created = proposer(
                actor=self.worker_id,
                description=goal,
                payload=payload,
            )
            return ([str(created)] if created else []), False

        branch_id = ""
        split_branch = getattr(self.graph, "split_branch", None)
        if callable(split_branch):
            specs = [
                {
                    "id": f"review-{sha1(part.encode('utf-8', 'ignore')).hexdigest()[:10]}",
                    "assumption": part[:180],
                    "prove_or_disprove": f"validate {part[:180]}",
                }
                for part in alternatives
            ]
            try:
                result = split_branch(
                    actor=self.worker_id,
                    title=f"Verify alternatives from fact {fact.fact_id}",
                    branches=specs,
                )
                branch_id = str((result or {}).get("branch_id") or "")
            except Exception:
                pass

        intent_ids: list[str] = []
        for alternative in alternatives:
            child_id = f"review-{sha1(alternative.encode('utf-8', 'ignore')).hexdigest()[:10]}"
            branch_payload = dict(payload)
            branch_payload.update(
                {
                    "review_branch": True,
                    "source_fact_id": int(fact.fact_id),
                    "branch_id": branch_id or child_id,
                    "route_hash": f"verify:{fact.fact_id}:{child_id}",
                }
            )
            created = proposer(
                actor=self.worker_id,
                description=(
                    f"{_VERIFY_INTENT_PREFIX} alternative: {alternative[:180]} "
                    f"(candidate fact #{fact.fact_id})"
                ),
                payload=branch_payload,
            )
            if created:
                intent_ids.append(str(created))
        return intent_ids, True

    def _apply_review(
        self,
        review_fact,
        fact_seq: int,
        *,
        action: str,
        reason: str,
        verification_goal: str = "",
        to_fact_seq: int | None = None,
    ) -> None:
        try:
            review_fact(
                actor=self.worker_id,
                fact_seq=int(fact_seq),
                action=action,
                reason=reason,
                verification_goal=verification_goal,
                to_fact_seq=to_fact_seq,
            )
        except Exception:
            pass


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
