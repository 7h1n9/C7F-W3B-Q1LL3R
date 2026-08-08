# Phase 2.4 Architecture Audit

Status: COMPLETED_WITH_LIMITS

Target: Asset Warranty Verification Platform at `http://192.168.236.1:28346/`

## Required chain

```text
Task -> Run -> Planner/Orchestrator -> Solver Runtime -> Agent Loop
     -> Tool Gateway -> Target -> Observation -> Evidence
     -> Completion Gate -> Final Answer
```

This document records verified code paths and integration gaps. It must not
claim end-to-end success before a real target run produces Completion Gate
evidence.

## Existing boundaries

- Legacy orchestration remains in `backend/app/orchestration/`.
- Solver v2 lives in `backend/app/solver/`.
- Production v2 entry is `SolverRuntimeService`.
- Runner and Tool Gateway remain execution boundaries.
- Blackboard state is persisted through the existing recovery checkpoint.
- Evidence authority and Completion Gate remain separate authorities.

## Verified audit

### Task and Run entry

- `backend/app/api/v1/runs.py` creates `SolveRun` records from the challenge
  and run request, then the start endpoint schedules
  `RunSupervisor.run_background()`.
- `backend/app/services/run_supervisor.py` routes `solver_mode ==
  "solver_v2"` to `SolverRuntimeService`; other modes remain on the legacy
  orchestrator path.

### Solver v2

- `backend/app/solver/service.py` constructs the production `RunContext`,
  `Blackboard`, `TaskStateMachine`, Planner, authorization boundary,
  `WorkerManager`, reducer, and `SolverLoop`.
- `backend/app/solver/blackboard/run_store.py` persists Solver control state
  in the existing `SolveRun.recovery_checkpoint_json`; no new schema is
  needed.
- `backend/app/solver/loop.py` persists action lifecycle records and typed
  audit objects in Blackboard history and projects observations into Solver
  knowledge.
- `backend/app/solver/action_lifecycle.py` detects an unfinished active
  action and records recovery feedback without silently retrying it.

### Execution and Evidence

- `backend/app/solver/worker/adapters/runner.py` currently calls
  `app.services.runner_client` directly. It does not pass through
  `backend/app/tools/gateway.py`.
- `ToolGateway.invoke()` is the existing durable execution boundary. It
  creates `ToolCall`, Runner execution records, `Artifact`, and `Observation`
  rows and emits tool lifecycle events.
- Existing `EvidenceLedger` records require an Artifact -> ToolCall ->
  AgentTask source chain. Solver v2 currently does not create an AgentTask or
  EvidenceLedger record after a Worker result.

### Completion and lifecycle

- `backend/app/solver/completion.py` already provides a read-only,
  dependency-injected Completion Evaluator, but `SolverRuntimeService` does
  not call it.
- `SolverRuntimeService._complete_unsolved()` currently transitions every
  bounded v2 run to `COMPLETED_UNSOLVED`; no verified Finding can produce
  `COMPLETED_SOLVED` through this path.
- `backend/app/solver/events.py` provides a strict Solver audit model, but the
  production service currently persists only `solver.run.started`,
  `solver.step.completed`, and terminal runtime events through
  `EventService`. Action audit objects remain in Blackboard history rather
  than the central RunEvent stream.

### Real target observation

- `http://192.168.236.1:28346/` responded with HTTP 200 and title
  `资产保修核验平台`.
- The page exposes `/help`, `/history`, and a JavaScript POST contract at
  `/api/warranty/check` with `asset_no` and `department` fields.
- Read-only valid and invalid control requests returned HTTP 200 with
  distinct `matched` values. No final answer is recorded here.

## Audit status

The entry and persistence boundaries are understood. The missing production
edges are documented in `missing_components.md`; implementation must preserve
the legacy path while making the Solver v2 path use the existing execution and
Evidence authorities.

## Phase 2.4 verified completion

- Real `solver_v2` runs reached `COMPLETED_SOLVED` on the authorized target.
- The Solver discovered the SQLite surface, confirmed the Boolean oracle, ran a bounded extraction script, reduced the result into a verified Finding, and passed the injected Evidence authority through the Solver Completion Gate.
- A fresh Runner session independently replayed the discovered predicate and returned `matched=true`; selected runs recorded `fresh_reproduction_verified=true`.
- Audit events retain identity, status, reason, and Evidence references without raw HTTP bodies, cookies, tokens, or ground truth.

## Remaining operational limit

Eight concurrent real runs produced six solved outcomes and two controlled `COMPLETED_UNSOLVED/SOLVER_NO_ACTION` outcomes after transient Runner errors. The reducer now permits one persisted script retry before terminal stop; a sequential retry succeeded. This is recorded as a load/recovery limitation, not as a solved result.
