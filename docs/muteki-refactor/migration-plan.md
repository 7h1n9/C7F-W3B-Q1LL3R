# Muteki Core Rewrite — Migration Plan

## Slice 1 — canonical contracts

- `muteki/graph.py`: append-only event log plus materialized facts/intents/
  dead-ends/flags/PoCs/resource claims.
- `muteki/skill/blackboard.py`: dependency-free worker CLI using
  `MUTEKI_BLACKBOARD_DB`, `MUTEKI_WORKER_ID`, and `MUTEKI_INTENT_ID`.
- `muteki/gate.py`: hardcoded flag provenance gate.
- `muteki/events.py`: ordered event envelope and replay cursor.

## Slice 2 — execution model

- `muteki/phases.py`: Prepare, Race, Coordinator, Finalize state machine.
- `muteki/workers.py`: engine profiles, one-shot process contract, and leases.
- `muteki/coordinator.py`: OODA scheduling, graph-change trigger, fast path,
  bounded capacity, and idempotent finalize.
- `muteki/reason.py`: injected cheap planner with bounded intent normalization.

## Slice 3 — adapters

- adapter from Muteki worker execution to the existing Tool Gateway/Runner
  boundary;
- adapter from Muteki event envelopes to the existing run-event/SSE stream;
- adapter from verified graph evidence to the existing Evidence authority and
  Completion Gate.

Adapters are the only place where the canonical Muteki layer may touch the
existing production lifecycle. They must be feature-flagged and reversible.

## Validation gates

1. Graph event replay reconstructs the same materialized state.
2. Two workers cannot claim one intent or exclusive resource simultaneously.
3. Worker CLI can operate with only stdlib and the shared SQLite path.
4. Race first flag cancels remaining workers and reaches Finalize once.
5. A graph change causes one replan; an unchanged graph does not.
6. Stop, timeout, worker crash, and solved runs all reach Finalize.
7. Existing Solver/legacy tests remain green.
8. Only after these pass is the production adapter enabled.
