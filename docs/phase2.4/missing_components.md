# Phase 2.4 Missing Components

Status: LIVE_VALIDATED_WITH_LIMITS

## Rules

- Do not infer completion from an agent message, an LLM phrase, or a fixed
  string.
- A solved result requires a verified Finding and valid Evidence references.
- Do not bypass Runner, Tool Gateway, Security, Evidence, or lifecycle
  authorities.
- Record every missing edge with evidence before implementing it.

## Findings

- [ ] Gateway-backed Solver Worker adapter. The current RunnerWorker uses the
  Runner client directly, so v2 does not produce the Tool Gateway's durable
  ToolCall/Artifact/Observation chain.
- [ ] Solver Evidence authority adapter. A v2 Worker result has
  `evidence_refs`, but no production code creates or verifies an
  `EvidenceLedger` reference for the result.
- [ ] Completion integration. `SolverRuntimeService` must evaluate the
  Blackboard through `SolverCompletionEvaluator` before selecting
  `COMPLETED_SOLVED`; bounded stops without a verified Finding must remain
  unsolved.
- [ ] Finding projection. The current Web reducer produces verified facts and
  hypotheses but no evidence-backed `knowledge.findings` record.
- [ ] Target-capable action planning. The v2 state machine exposes
  `mysql_metadata_discovery` and `sql_extract`, while the current v2 Runner
  Worker supports only `http_request` and `sql_boolean_compare`.
- [ ] Central audit coverage. Action-level typed events are currently in
  Blackboard history; production EventService coverage is incomplete.
- [ ] Recon/Exploit/Verification/Evidence agent wiring. Existing role helpers
  are used by legacy orchestration and are not connected to Solver v2.
- [ ] Phase 2.4 integration tests and real-run pressure sample.

## Resolution checkpoint

The listed gaps were closed for the explicit Solver v2 route without changing the legacy orchestrator, Runner internals, Tool Gateway schema, Evidence Store schema, or database schema:

- Gateway Worker invokes the existing Tool Gateway and records durable ToolCall, Artifact, Observation, and EvidenceLedger records.
- Solver Evidence authority verifies EvidenceLedger references attached to Solver Findings.
- Completion is decided only by `SolverCompletionEvaluator`; database/table/schema facts and ordinary configuration values remain unsolved.
- The reducer and Planner support bounded SQLite extraction and one persisted retry after transient script errors.
- Strict Solver audit events are projected to central RunEvents.

The remaining issue is operational: concurrent Runner load can make a bounded script inconclusive. Higher concurrency requires Runner/database capacity tuning outside this scoped refactor.

## Muteki validation status (2026-08-09)

The canonical Muteki production adapter is live-validated against the
configured Challenge target on port `28656`. It uses the canonical graph as
its completion authority and persists a sanitized `SolveRun.report_json`; it
does not manufacture legacy `FlagCandidate` rows or route through the legacy
formal report barrier. Host Codex health passed. Container health remains
fail-closed until a proprietary engine CLI and credentials are intentionally
installed in the worker image. The one fresh Run proves the chain; the
ten-run pressure campaign remains pending.
