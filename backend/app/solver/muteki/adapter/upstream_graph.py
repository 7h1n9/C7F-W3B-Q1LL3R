"""Non-production bridge from the current Muteki graph to upstream Muteki.

This adapter is intentionally a projection, not a second runtime state
authority.  The current graph remains authoritative for existing runs; the
upstream ``SolveGraph`` is a temporary compatibility view used while the
official source is migrated in slices.
"""

from __future__ import annotations

from typing import Any

from ..graph import MutekiGraph
from ..upstream_bridge import open_upstream_shared_graph, to_upstream_challenge


def project_graph(graph: MutekiGraph, challenge: Any) -> Any:
    """Project current graph facts into an upstream ``SolveGraph`` view."""

    from muteki.models.solve_graph import SolveGraph

    projected = SolveGraph(challenge=to_upstream_challenge(challenge))
    for fact in graph.facts():
        refs = list(fact.evidence_refs)
        projected.add_evidence(
            source=fact.source_worker_id,
            fact=fact.content,
            artifact_id=refs[0] if refs else None,
            verified=fact.verified,
            source_solver=fact.source_worker_id,
            confidence=1.0 if fact.verified else 0.5,
            witness="evidence_ref" if refs else None,
            verifier="current-muteki-graph" if fact.verified else "",
        )
    for dead_end in graph.dead_ends():
        projected.mark_dead_end(dead_end.description)
    for flag in graph.flags(verified_only=True):
        projected.add_flag(flag.flag_value)
    return projected


__all__ = [
    "open_upstream_shared_graph",
    "project_graph",
    "to_upstream_challenge",
]
