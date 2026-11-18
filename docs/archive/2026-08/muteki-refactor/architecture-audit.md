# Muteki Core Rewrite — Architecture Audit

Date: 2026-08-08
Branch: `refactor-muteki-core`

## Reference invariants

The canonical design follows Project Muteki's documented control flow:

1. `Prepare` creates an isolated per-run workspace and blackboard.
2. Cold-start `Recon Race` launches one-shot heterogeneous workers in parallel.
3. If race does not solve the challenge, `Coordinator OODA` reads the complete
   graph, asks `Reason` for bounded intents, dispatches claimable intents, and
   waits for graph changes before replanning.
4. `Finalize` is idempotent and is reached for solved, stopped, budget-exhausted,
   and crashed runs.

The worker-to-board channel is the stdlib-only `blackboard.py` skill. Workers do
not call Coordinator or talk to one another directly. A flag is accepted only
after format validation and provenance from real worker output.

## Current project gaps

| Concern | Current state | Rewrite requirement |
|---|---|---|
| SharedGraph source of truth | Mutable SQLite tables with side-effect events | Append-only event log; query tables are rebuildable projections |
| Worker boundary | In-process async runner callback | One-shot CLI/process contract; only skill reads/writes the graph |
| Phases | Partial coordinator phase enum | Explicit Prepare/Race/Coordinator/Finalize transitions and terminal convergence |
| Race | Bootstrap callback only | One worker per healthy engine, fast path on first gated flag |
| Reason | Injected provider plus fallback | Cheap, graph-summary-only planner; bounded non-overlapping intents |
| Claims | Intent/resource claims exist | Atomic lease claims, expiry, release on finalize, no duplicate high-cost activity |
| Review | No parallel review lane | Non-blocking review worker that can challenge facts and suppress routes |
| Event stream | New local JSONL bus is not API-wired | One ordered run stream, replayable by cursor, bridged to existing SSE only once |
| Production integration | Existing `solver_v2` remains authoritative | Add an adapter; do not create a second RunStatus/Evidence authority |

## Replacement boundary

The new canonical layer will live under `backend/app/solver/muteki/` and own
coordination state for runs that explicitly select the Muteki mode. Existing
`solver_v2`, legacy Orchestrator, Runner, Tool Gateway, Evidence Store, and
database schema remain compatibility boundaries until a fresh end-to-end
integration proves the replacement path.

The existing `backend/app/solver/shared_graph/` is treated as an interim
compatibility implementation. It must not be silently presented as complete
Muteki parity.

## Non-goals

- No deletion of `multi_agent_orchestrator.py`.
- No rewrite of Runner internals or Tool Gateway protocol.
- No new application database tables for the canonical graph.
- No direct copying of challenge ground truth into worker context.
- No automatic acceptance of model-reported flags.
