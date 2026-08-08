# Canonical Muteki production adapter design

## Boundary

The canonical runtime is opt-in through `solver_mode=muteki`. The existing
`multi_agent_v1`, `solver_v2`, and legacy orchestrator paths remain the
defaults and are not replaced.

```text
Muteki WorkerJob
  -> ToolAdapter
  -> GatewayWorker
  -> ToolGateway
  -> existing RunnerClient
  -> Artifact / Observation / EvidenceLedger
  -> sanitized Fact + evidence references
  -> MutekiGraph
```

`RunnerAdapter` is a separate normalized facade for `python_run` and
`script_run` jobs. It uses the existing `RunnerClient.create_job` and
`wait_job` methods and never changes Runner internals.

## State and evidence ownership

- `MutekiGraph` owns canonical coordination state, intents, facts, dead ends,
  and Gate decisions in its per-run SQLite graph.
- Artifact, Observation, ToolCall, and EvidenceLedger remain authoritative for
  raw execution evidence.
- `EvidenceAdapter` validates and preserves existing evidence references; it
  does not create a parallel evidence schema.
- `EventBridge` persists sanitized canonical events through the existing
  `event_service`, which is already the source for the existing SSE endpoint.

## API and lifecycle

Creating a run with `solver_mode=muteki` opts into the new branch. Starting the
run still uses `POST /api/v1/runs/{run_id}/start`; the supervisor selects the
Muteki runtime only for that mode. The default `solver_mode=multi_agent_v1`
and explicit `solver_v2` behavior remain unchanged.

The bridge uses the existing per-run event sequence allocated by
`event_service`. The canonical graph sequence is retained as
`muteki_sequence` in the event payload for correlation and is deduplicated in
the bridge before publication.

## Safety constraints

- No Runner implementation changes.
- No Tool Gateway implementation changes.
- No EvidenceLedger schema changes.
- No legacy orchestrator deletion or replacement.
- No raw response, cookie, token, secret, or ground-truth value is written to
  canonical audit event payloads.
