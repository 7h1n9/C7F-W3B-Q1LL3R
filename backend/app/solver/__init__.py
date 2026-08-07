"""State-driven Solver Core skeleton.

This package is intentionally isolated from the legacy orchestrators.  Phase
1.1 defines contracts and a single-tick control loop; migration adapters are
out of scope until the skeleton is reviewed and verified.
"""

from .action import ActionIntent
from .coordinator import Coordinator, CoordinatorStep
from .events import SolverEvent
from .knowledge import KnowledgeStore
from .loop import SolverLoop, SolverLoopStep
from .observation import SolverObservation
from .planner import DeterministicPlanner, NoopPlanner, Planner, SolverIntent
from .policy import ActionPolicyValidator, PolicyDecision, PolicyResult
from .reducers import KnowledgeUpdate, WebObservationReducer
from .state_machine import SolverPhase, TaskStateMachine
from .worker import MockWorker, NoopWorker, Worker, WorkerResult

__all__ = [
    "Coordinator",
    "CoordinatorStep",
    "ActionIntent",
    "ActionPolicyValidator",
    "DeterministicPlanner",
    "MockWorker",
    "KnowledgeStore",
    "KnowledgeUpdate",
    "NoopPlanner",
    "PolicyDecision",
    "PolicyResult",
    "NoopWorker",
    "Planner",
    "SolverIntent",
    "SolverEvent",
    "SolverLoop",
    "SolverLoopStep",
    "SolverObservation",
    "SolverPhase",
    "TaskStateMachine",
    "Worker",
    "WorkerResult",
    "WebObservationReducer",
]
