"""Durable, worker-facing collaboration graph for the multi-worker layer.

The graph is deliberately separate from the Solver Blackboard.  The Solver
Blackboard remains the authoritative control state for the production
``solver_v2`` runtime; this package provides an additive coordination surface
for one-shot workers.
"""

from .blackboard import (
    DeadEnd,
    Fact,
    Flag,
    Intent,
    PoC,
    ResourceClaim,
    SharedGraph,
)
from .bus import EVENT_TYPES, EventEnvelope, InsightBus, SolverEventBus
from .gate import FlagGate, GateDecision

__all__ = [
    "DeadEnd",
    "EVENT_TYPES",
    "EventEnvelope",
    "Fact",
    "Flag",
    "FlagGate",
    "GateDecision",
    "InsightBus",
    "Intent",
    "PoC",
    "ResourceClaim",
    "SharedGraph",
    "SolverEventBus",
]
