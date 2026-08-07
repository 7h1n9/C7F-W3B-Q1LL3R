"""State-driven Solver Core skeleton.

This package is intentionally isolated from the legacy orchestrators.  Phase
1.1 defines contracts and a single-tick control loop; migration adapters are
out of scope until the skeleton is reviewed and verified.
"""

from .coordinator import Coordinator, CoordinatorStep
from .planner import NoopPlanner, Planner, SolverIntent
from .state_machine import SolverPhase, TaskStateMachine
from .worker import NoopWorker, Worker, WorkerResult

__all__ = [
    "Coordinator",
    "CoordinatorStep",
    "NoopPlanner",
    "NoopWorker",
    "Planner",
    "SolverIntent",
    "SolverPhase",
    "TaskStateMachine",
    "Worker",
    "WorkerResult",
]
