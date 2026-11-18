# 2026-07-22 Solver Regression Baseline

This is a redacted regression record. It is not a lesson or a model prompt.

## Pre-fix runs

| Run | Engine | Status | Attempt | SolverState | Flag | Logical calls | ToolCall rows | AttackChain | Capability Ledger |
|---|---|---|---|---|---|---:|---:|---|---|
| `b098dc5a-5c5a-438e-9ac9-05af8577f420` | Codex | `COMPLETED_SOLVED` | `FAILED_ENGINE` | `FAILED_ENGINE` | verified | 54 | 182 | N1 | empty |
| `96afc357-bc9b-48fc-8bd3-189787d00518` | Codex | `COMPLETED_SOLVED` | `FAILED_ENGINE` | `FAILED_ENGINE` | verified | ~72 | 123 | N1 | report before verification; `verified_flag_count=0` |
| `3df6515e-2104-4189-99ee-8fbd712409f3` | OpenAI | — | — | — | — | 18 | — | — | 93 steps; 65 requested; 49 rejected; 52 no-progress; 23 AgentTurn; 18 file reads; default objective 23; default hypothesis 23 |

The first two runs are the repaired-state regression cases. The third is the OpenAI duplicate-`file_read` and degraded-action fixture.

## Required invariants

- `VERIFIED_FLAG` dominates stream and provider errors.
- A verified run is `COMPLETED_SOLVED` in Run, Attempt, and SolverState, and cannot resume.
- Late tool/agent events are retained as post-terminal audit data and cannot create ToolCall, Observation, Artifact, or state transitions.
- Reports are generated only after terminal state, flag review, closed Attempt, completed key ToolCalls, and frozen terminal generation.
- Tool budget and reports count logical calls; Runner/Gateway/Materializer events are execution traces.
- Repeated bounded file reads return the original model view with `error_code=null` and `FILE_RANGE_ALREADY_AVAILABLE` warning.
- SQL automation is bounded to the Challenge Host, at most 10 columns, 40 requests, and concurrency 2.

## Redacted fixture

See `backend/tests/fixtures/historical_runs/2026-07-22-solver-regression.json`.
