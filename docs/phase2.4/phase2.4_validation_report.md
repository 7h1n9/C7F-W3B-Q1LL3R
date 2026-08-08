# Phase 2.4 Validation Report

Status: COMPLETED_WITH_LIMITS

Date: 2026-08-08

Target: `资产保修核验平台` — `http://192.168.236.1:28346/`

## Result

The explicit `solver_v2` production path completed the authorized target
without a supplied vulnerability location or answer. A real run autonomously:

1. created a Run through the API and was routed by `RunSupervisor`;
2. initialized and persisted Blackboard state;
3. confirmed the target HTTP surface and a Boolean SQL oracle;
4. used the existing Tool Gateway and remote Runner;
5. recorded ToolCall, Artifact, Observation, and verified EvidenceLedger rows;
6. enumerated the SQLite surface and extracted a flag-shaped value through a
   bounded script;
7. reduced the observation into a verified Solver Finding;
8. passed `SolverCompletionEvaluator` with valid Evidence references;
9. materialized the verified candidate and emitted `solver.run.completed`;
10. independently replayed the predicate in a fresh Runner session and
    received `matched=true`.

The final answer is the verified candidate stored in the protected run
Evidence/report artifacts. The report renderer redacts it from Markdown.

## Chain evidence

Representative successful Run: `244d0056-636d-4b5f-92e7-beb432ca757a`

- Solver status: `COMPLETED_SOLVED`
- Tool calls: 32
- Evidence rows: 10, all `VERIFIED`
- Candidate: pattern matched, `verified=true`, `review_state=VALID`
- `thread_invalidated=true`
- `solver.completion.evaluated`: present
- `solver.run.completed`: present
- `report.completed`: present
- Fresh reproduction: `verified=true`, HTTP response `matched=true`

The successful run's audit stream contains `run.created`,
`solver.run.started`, action planned/authorized/started/completed events,
`solver.tool.called`, `solver.observation.received`, tool lifecycle events,
`solver.completion.evaluated`, `run.generation_terminal`,
`solver.run.completed`, `report.started`, and `report.completed`.

## Recon report

- Target host: `192.168.236.1`
- Entry point: `/`
- Business endpoint: `POST /api/warranty/check`
- Observed fields: `asset_no`, `department`
- Server: Werkzeug/Python
- Surface: `/help`, `/history`, and the warranty-check API
- Risk: the `department` input produced a stable true/false SQL differential
  through the authorized Boolean oracle.

## Attack trace summary

```text
http_request
  -> sql_boolean_compare
  -> oracle_expression_calibration
  -> request_capture
  -> sqlmap_detect / bounded metadata attempts
  -> sqlite metadata discovery
  -> bounded script_run
  -> verified Finding
  -> Completion Gate
  -> fresh http_request reproduction
```

Raw HTTP bodies and sensitive values are not copied into Solver audit events;
the Evidence rows point to protected artifacts.

## Real-run sample

| Category | Runs | Solved | Unsolved | Notes |
|---|---:|---:|---:|---|
| Initial genuine runs | 3 | 3 | 0 | established the reproducible path |
| Eight-way concurrent load | 8 | 6 | 2 | two bounded stops after transient Runner errors |
| Sequential recovery retry | 1 | 1 | 0 | retry path and fresh reproduction passed |
| Final post-validation run | 1 | 1 | 0 | fresh run and fresh reproduction passed |
| Total | 13 | 11 | 2 | solved rate 84.6% across mixed load |

Successful Run IDs:

- `4b9236c2-1b27-4afc-8b0b-9a7b522eb5b2`
- `767bed39-7ea7-4742-b62c-e82bb0975928`
- `244d0056-636d-4b5f-92e7-beb432ca757a`
- `098a9e41-16b2-4c57-bbac-bd0196c0b5a0`
- `21b23595-e87f-4583-b241-8796edef2c53`
- `85713c65-4652-4f9c-85ef-7ab2634a44cc`
- `9f16d74f-8e50-406d-a394-2c44f5f17b29`
- `b31c9530-b3c9-4054-b202-9fb1bac21eb5`
- `e1dc1aaf-36ca-43b2-b518-5602fa14a407`
- `da0da023-8a0d-41fb-9bb9-6b8d2f76b5ac`
- `b69fad11-b97a-4ff3-9351-7a9e1844d1c1`

Controlled unsolved Run IDs:

- `f35fad5f-e437-4fa3-9007-610c8020e123`
- `fd7fc60e-cff3-44a6-a5ad-47e36d1ed613`

Both unsolved runs had no verified Finding and therefore correctly failed the
Completion Gate. Their bounded script returned only seven requests and one
transient error under concurrent load. The new persisted retry path was then
validated by the sequential recovery Run above.

Recovery count: no silent action replay; the real sample did not contain an
interrupted action. The bounded retry path is covered by the integration
tests, while the sequential recovery and final post-validation runs both
completed successfully. Token consumption is not exposed by the current
`codex_sdk` bridge and is reported as `N/A`, not inferred.

## Tests and static validation

- Phase 2.4 and Solver integration selection: `60 passed`.
- Full backend suite: `362 passed`.
- `python -m compileall -q app`: passed.
- Targeted Ruff for modified Solver/runtime/report files: passed.
- `git diff --check`: passed.

## Remaining issues

- Eight-way Runner concurrency can exhaust target/Runner response capacity and
  cause bounded extraction to stop without a Finding. One retry is now
  persisted and bounded; larger capacity work belongs to Runner/database
  operations, outside this refactor scope.
- The current bridge does not expose token counts.
- Legacy orchestrator remains intact and is not replaced by this validation.
