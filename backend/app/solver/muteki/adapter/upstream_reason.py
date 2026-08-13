"""Adapter for the official Muteki Reason JSON protocol."""

from __future__ import annotations

from typing import Any


class UpstreamReasonProposals(list[dict[str, Any]]):
    """List-compatible projection of the official Reason result.

    The upstream protocol returns a decision envelope, not only an Intent
    array.  Keeping this object list-compatible preserves the existing local
    provider contract while carrying the official ``verdict`` and ``drift``
    fields to the local Reason boundary.
    """

    def __init__(
        self,
        values: list[dict[str, Any]],
        *,
        goal_met: bool = False,
        verdict: str = "explore",
        drift: str = "",
        complete_why: str = "",
    ) -> None:
        super().__init__(values)
        self.goal_met = bool(goal_met)
        self.verdict = str(verdict or "explore")
        self.drift = str(drift or "")
        self.complete_why = str(complete_why or "")


def parse_upstream_reason_reply(reply: str, *, max_intents: int = 4) -> UpstreamReasonProposals:
    """Convert official Reason output without dropping its decision envelope."""

    from muteki.solver.reason import parse_reason_reply

    result = parse_reason_reply(str(reply or ""), max_intents=max_intents)
    proposals: list[dict[str, Any]] = []
    for intent in result.intents:
        proposals.append(
            {
                "goal": intent.goal,
                "worker_class": intent.worker_class,
                "rationale": intent.rationale,
                "payload": {
                    "route_hash": intent.route_hash,
                    "branch_id": intent.branch_id,
                    "lane_key": intent.lane_key,
                    "risk_class": intent.risk_class,
                    "resource_key": intent.resource_key,
                    "depends_on": list(intent.depends_on),
                    "from_facts": list(intent.from_facts),
                },
            }
        )
    return UpstreamReasonProposals(
        proposals,
        goal_met=bool(result.goal_met),
        verdict=str(result.verdict or "explore"),
        drift=str(result.drift or ""),
        complete_why=str(result.complete_why or ""),
    )


__all__ = ["UpstreamReasonProposals", "parse_upstream_reason_reply"]
