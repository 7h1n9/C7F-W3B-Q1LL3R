"""Canonical Muteki-style coordination contracts.

This package is intentionally not wired into the existing production Solver
entry yet.  It provides the event-sourced graph and one-shot worker boundary
needed for the staged migration.
"""

from .container_exec import ContainerExecutor, ContainerResult, SkillResult
from .control import ControlClient, ControlMessage, ControlReceiver, InMemoryControlBus
from .coordinator import CoordinatorConfig, MutekiCoordinator
from .core.stage_policy import StagePolicy
from .events import EventEnvelope, EventType
from .gate import GateDecision, MutekiFlagGate
from .graph import DeadEnd, Fact, Flag, Intent, MutekiGraph, PoC, ResourceClaim
from .identity import EngineType, IdentityModel
from .phases import MutekiPhase, PhaseDecision
from .reason import IntentProposal, MutekiReason, ReasonResult
from .worker import WorkerEngine, WorkerPool, WorkerResult
from .workers import EngineProfile, MutekiWorkerPool, WorkerJob, WorkerOutcome
from .workspace import MutekiWorkspace


def __getattr__(name: str):
    if name in {"CLIDriver", "ProcessResult"}:
        from .cli_driver import CLIDriver, ProcessResult

        return {"CLIDriver": CLIDriver, "ProcessResult": ProcessResult}[name]
    raise AttributeError(name)

__all__ = [
    "DeadEnd",
    "CLIDriver",
    "ContainerExecutor",
    "ContainerResult",
    "ControlClient",
    "ControlMessage",
    "ControlReceiver",
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
    "InMemoryControlBus",
    "MutekiWorkspace",
    "MutekiPhase",
    "PhaseDecision",
    "ProcessResult",
    "PoC",
    "EngineProfile",
    "EngineType",
    "IdentityModel",
    "IntentProposal",
    "ReasonResult",
    "ResourceClaim",
    "WorkerJob",
    "WorkerOutcome",
    "StagePolicy",
    "WorkerEngine",
    "WorkerPool",
    "WorkerResult",
    "SkillResult",
]
