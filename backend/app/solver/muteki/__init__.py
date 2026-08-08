"""Canonical Muteki-style coordination contracts.

This package is intentionally not wired into the existing production Solver
entry yet.  It provides the event-sourced graph and one-shot worker boundary
needed for the staged migration.
"""

from .coordinator import CoordinatorConfig, MutekiCoordinator
from .core.stage_policy import StagePolicy
from .events import EventEnvelope, EventType
from .gate import GateDecision, MutekiFlagGate
from .graph import DeadEnd, Fact, Flag, Intent, MutekiGraph, PoC, ResourceClaim
from .phases import MutekiPhase, PhaseDecision
from .reason import IntentProposal, MutekiReason, ReasonResult
from .worker import WorkerEngine, WorkerPool, WorkerResult
from .workers import EngineProfile, MutekiWorkerPool, WorkerJob, WorkerOutcome
from .workspace import MutekiWorkspace

__all__ = [
    "DeadEnd",
    "CoordinatorConfig",
    "EventEnvelope",
    "EventType",
    "Fact",
    "Flag",
    "GateDecision",
    "Intent",
    "MutekiFlagGate",
    "MutekiGraph",
    "MutekiCoordinator",
    "MutekiReason",
    "MutekiWorkerPool",
    "MutekiWorkspace",
    "MutekiPhase",
    "PhaseDecision",
    "PoC",
    "EngineProfile",
    "IntentProposal",
    "ReasonResult",
    "ResourceClaim",
    "WorkerJob",
    "WorkerOutcome",
    "StagePolicy",
    "WorkerEngine",
    "WorkerPool",
    "WorkerResult",
]
