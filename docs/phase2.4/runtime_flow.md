# Phase 2.4 Runtime Flow

Status: COMPLETED_WITH_LIMITS

## Intended flow

```text
User task
  -> challenge/run creation
  -> run start
  -> supervisor routing
  -> SolverRuntimeService
  -> durable Blackboard
  -> Planner
  -> authorization/policy
  -> WorkerManager
  -> RunnerWorker / Tool Gateway
  -> target observation
  -> reducer / knowledge
  -> Evidence authority
  -> Completion Gate
  -> final RunStatus and answer
```

## Verified call sites

- Task creation/start: `backend/app/api/v1/runs.py` ->
  `RunSupervisor.run_background()`.
- v2 routing: `backend/app/services/run_supervisor.py` ->
  `SolverRuntimeService.run()`.
- v2 loop construction: `backend/app/solver/service.py` creates
  `SolveRunBlackboardStore`, `DeterministicPlanner`, `TaskStateMachine`,
  `WorkerManager`, `WebObservationReducer`, and `SolverLoop`.
- durable state: `backend/app/solver/blackboard/run_store.py` stores the
  serialized Blackboard under the existing recovery checkpoint JSON.
- loop order: `backend/app/solver/loop.py` performs READ -> allowed actions ->
  Planner -> policy/security authorization -> action checkpoint -> Worker ->
  Observation -> reducer -> Blackboard update.
- legacy execution boundary: `backend/app/tools/gateway.py::ToolGateway.invoke`
  persists ToolCall/Artifact/Observation and emits tool events.
- legacy evidence boundary: `backend/app/services/multi_agent.py::EvidenceLedgerService.record`
  validates the source chain and persists EvidenceLedger.
- legacy completion boundary: `backend/app/services/finish_gate.py` remains
  independent; Solver v2 now evaluates completion through
  `backend/app/solver/completion.py` with the injected Evidence authority.

## Current broken edges

```text
Solver v2 Worker -> runner_client       (Tool Gateway bypass)
Worker result -> SolverObservation       (no EvidenceLedger reference)
Solver knowledge -> _complete_unsolved   (Completion Gate bypass)
Solver audit -> Blackboard history       (not all central RunEvents)
```

These were the initial audit findings. They were closed for the production `solver_v2` path during Phase 2.4; the legacy path remains unchanged.

## Verified real execution

```text
Task/Run API
  -> RunSupervisor(solver_v2)
  -> SolverRuntimeService
  -> Blackboard + Planner + ActionAuthorizer
  -> WorkerManager/GatewayWorker
  -> Tool Gateway -> Runner -> target
  -> Artifact/Observation -> EvidenceLedger
  -> Reducer/Knowledge Finding
  -> SolverCompletionEvaluator
  -> COMPLETED_SOLVED + verified stop
  -> fresh HTTP reproduction -> report.completed
```

Observed target: `http://192.168.236.1:28346/`.
