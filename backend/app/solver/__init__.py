"""State-driven Solver Core skeleton.

This package is intentionally isolated from the legacy orchestrators.  Phase
1.1 defines contracts and a single-tick control loop; migration adapters are
out of scope until the skeleton is reviewed and verified.
"""

from .action import ActionIntent
from .action_lifecycle import (
    ActionExecutionRecord,
    ActionExecutionState,
    find_interrupted_action,
    generate_fingerprint,
    validate_retry_relationship,
)
from .classification import (
    LLMClassifierConfig,
    LLMClassifierError,
    LLMVulnerabilityClassifier,
    VulnerabilityClassifier,
)
from .context import ChallengeContext, RunContext, RunLimits, RuntimeUsage, TargetContext
from .context_factory import RunContextFactory
from .coordinator import Coordinator, CoordinatorStep
from .events import SolverEvent
from .gate import FlagGate, GateDecision
from .knowledge import KnowledgeStore
from .lifecycle import LifecycleDecision, SolverLifecycleMapper, SolverLifecycleOutcome
from .loop import SolverLoop, SolverLoopStep
from .multi_worker import CoordinationPhase, MultiWorkerCoordinator
from .observation import SolverObservation
from .planner import DeterministicPlanner, NoopPlanner, Planner, SolverIntent
from .policy import ActionPolicyValidator, PolicyDecision, PolicyResult
from .reason import ReasonIntent, ReasonPlanner
from .reducers import KnowledgeUpdate, WebObservationReducer
from .shared_graph import SharedGraph, SolverEventBus
from .state_machine import SolverPhase, TaskStateMachine
from .worker import (
    MockWorker,
    NoopWorker,
    RunnerAdapter,
    RunnerWorker,
    Worker,
    WorkerManager,
    WorkerResult,
    WorkerUnavailable,
)

__all__ = [
    "Coordinator",
    "CoordinatorStep",
    "ActionIntent",
    "ActionExecutionRecord",
    "ActionExecutionState",
    "ChallengeContext",
    "RunContext",
    "RunLimits",
    "RuntimeUsage",
    "VulnerabilityClassifier",
    "LLMClassifierConfig",
    "LLMClassifierError",
    "LLMVulnerabilityClassifier",
    "RunContextFactory",
    "TargetContext",
    "ActionPolicyValidator",
    "DeterministicPlanner",
    "MockWorker",
    "KnowledgeStore",
    "LifecycleDecision",
    "KnowledgeUpdate",
    "NoopPlanner",
    "PolicyDecision",
    "PolicyResult",
    "NoopWorker",
    "RunnerAdapter",
    "RunnerWorker",
    "Planner",
    "SolverIntent",
    "SolverEvent",
    "FlagGate",
    "GateDecision",
    "SharedGraph",
    "SolverEventBus",
    "ReasonIntent",
    "ReasonPlanner",
    "CoordinationPhase",
    "MultiWorkerCoordinator",
    "SolverLoop",
    "SolverLoopStep",
    "SolverLifecycleMapper",
    "SolverLifecycleOutcome",
    "SolverObservation",
    "SolverPhase",
    "TaskStateMachine",
    "Worker",
    "WorkerManager",
    "WorkerResult",
    "WorkerUnavailable",
    "WebObservationReducer",
    "find_interrupted_action",
    "generate_fingerprint",
    "validate_retry_relationship",
]
