# Phase 2.4 Validation Report

Status: LIVE_VALIDATED_WITH_LIMITS

Date: 2026-08-09

Target: `设备报修工单平台` — `http://192.168.236.1:28656/`

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

## Authoritative Muteki live run (2026-08-09)

Run ID: `234d17a5-0c48-4cd0-94ea-11109607706e`

- Mode: `muteki`; result: `SUCCESS` / `COMPLETED_SOLVED`.
- Target: `http://192.168.236.1:28656/` (the configured Challenge target;
  the prompt's port `28346` was stale).
- Execution: 16 completed ToolCalls through the existing ToolGateway and
  remote Runner adapter, with 16 Evidence references.
- Race: bounded endpoint reconnaissance, same-session authentication and
  ticket/API exploration produced `IDOR` classification with confidence 85.
- Lifecycle: `prepare -> race -> coordinator -> finalize`, followed by
  `muteki.run_finished` and outer `run.completed`.
- Completion: canonical graph flag record verified; durable `report_json`
  contains `flag_verified=true`, Evidence references, graph path, and stage
  list. The flag value is intentionally redacted from this report.
- Frontend: browser validation showed Muteki mode, status `已解出`, phase
  `FINALIZE`, Muteki facts, and the live audit timeline.

## Attempts and fixes

- EventBridge ordering/isolation and Gateway rollback handling fixed the
  initial Race transaction overlap.
- Coordinator revision handling restored the Reason pass after Worker facts.
- An immutable Challenge snapshot removed the post-commit `MissingGreenlet`
  failure.
- Bounded adjacent-ID exploration and whitespace-tolerant report URL parsing
  completed the authorized IDOR path.
- Muteki terminal detail reads now skip the legacy Codex report barrier;
  RunSupervisor persists the Muteki report JSON instead.

## Limits

- The ten-run pressure requirement has not been completed in this checkpoint;
  no aggregate success rate is claimed.
- Host Codex CLI health passed. Container health remains unavailable for model
  execution because the worker image has no proprietary Codex/Claude/Cursor
  CLI or credentials.

## Historical document preview center live validation (2026-08-09)

Challenge: `f773ad47-b80d-49e3-8254-9e0dfe11c582`
Target: `http://192.168.236.1:28252`
Mode: `solver_mode=muteki`

The first bounded runs exposed two real integration issues: Race did not
follow document links deeply enough to reach the preview route, and the
Coordinator dispatched an intent without claiming it first. Both were fixed
without changing Runner, Tool Gateway, Evidence schema, or the legacy
Orchestrator. The path planner was then extended with bounded legacy preview
variants derived from the target's disclosed archive reference. The successful
variant was executed by the normal Runner/Tool Gateway path; the raw flag is
intentionally omitted here.

Validation runs:

- `5116e337-8b99-40a8-8c72-a21ee7bcd6e8`: controlled `COMPLETED_UNSOLVED`;
  classification was still `GENERIC_WEB` because the nested document links
  were not yet reached.
- `8a4a0b42-48a8-43f0-9a4f-067611cdc2b5`: controlled unsolved outcome;
  nested preview reconnaissance and `PATH_TRAVERSAL` classification worked,
  but the unclaimed-intent lifecycle prevented the exploit worker from
  progressing.
- `6a53bbe3-e8e8-464c-86a0-f9a95c5ccf67`: controlled unsolved outcome after
  intent claiming was fixed; the first two legacy encodings were denied by
  the target.
- `73eca08b-2b4f-49c6-8fad-c8914df1f3c0`: `COMPLETED_SOLVED`; the graph
  contained one verified flag and all three generated intents were done.
- `076a0026-043c-493a-bedc-179c799999bb`: fresh reproduction,
  `COMPLETED_SOLVED`; 40 graph facts, 3 completed path-traversal intents, 1
  verified graph flag, and 18 Evidence references.
- `68b41c1a-69fd-42ed-89d5-bdf8b165ee29`: final lifecycle reproduction,
  `COMPLETED_SOLVED`; outer `current_phase=REPORTING`, formal report available,
  `flag_verified=true`, and 18 Evidence references.

The fresh Run also verified the formal report path. `GET /api/v1/runs/{id}/report`
returned `result=solved`, `status=COMPLETED_SOLVED`, `flag_verified=true`, and
18 Evidence references. A Muteki-specific report materializer was added so
canonical graph completion produces the existing report/report_json artifacts
without fabricating legacy FlagCandidate rows. The report excludes raw HTTP
responses and sensitive session material.

Current target result: SUCCESS. The requested ten-run pressure campaign and
container-proprietary-engine validation remain separate, explicitly deferred
limits.

## Strategy coverage follow-up (2026-08-09)

The initial “only historical preview is solvable” symptom was traced to an
incomplete production Reason provider, not to the Muteki graph itself. Before
this follow-up, its non-legacy branches ended with no plan after Race. The
canonical runtime now selects bounded actions from Blackboard facts for the
remaining classified web categories and for the asset SQL path, while keeping
IDOR and preview-specific branches intact.

The asset target was validated through three fresh controlled Runs. The chain
reached two-field Boolean validation, calibration, and metadata discovery. It
correctly stopped as `COMPLETED_UNSOLVED` when the target supplied no verified
table/database fact; this is a target capability result, not a claim of
successful extraction. Fresh Device Warranty Run
`e0c105e1-d686-48e2-8ae3-c76c1719d518` reached `COMPLETED_SOLVED`, confirming
that the generalized strategy layer did not regress the existing IDOR chain.

Focused validation after the strategy changes: `35 passed`; compileall,
targeted Ruff, and `git diff --check` passed. The full backend suite and a
multi-target pressure campaign were not rerun in this checkpoint.

After that focused run, the full backend suite completed with `420 passed,
4 failed`. The four failures are the existing legacy Boolean dispatch tests;
their failure cause is unavailable local MySQL (`asyncmy` cannot connect to
`localhost`), not a Muteki assertion or a new Runtime failure. The full run
also regenerated the tracked benchmark SQLite binary, which remains visible
as an uncommitted test side effect and was not overwritten automatically.

## Upstream Graph fresh-run checkpoint (2026-08-09)

The official Muteki `SQLiteSharedGraph` facade was exercised through a
temporary backend with `APP_MUTEKI_GRAPH_BACKEND=upstream`. The normal
production backend remained on its default graph backend.

- Historical preview target run `81e3a96e-6847-443b-8b5a-457c144eab1a`:
  `TARGET_UNAVAILABLE`; the target returned an empty HTTP reply before a
  solver observation could be recorded.
- Reachable email-template target run
  `a69d9f6d-a955-40d4-b536-64eb5fe5690d`:
  `COMPLETED_UNSOLVED`, classification `SSTI`, 22 Tool Calls, 22 Evidence
  references, and a complete report/SSE trail. No SQL tool call was emitted.
- The first run on that target (`b4465c09-52c5-4fd6-8621-5d0afff9bcb4`)
  exposed and fixed a false IDOR classification for a 404 candidate endpoint.

This validates the migrated Graph/Coordinator/Runner/Evidence/Completion
chain in a live run, but does not claim that the new email-template target is
solved. The remaining gap is a generic authenticated-session continuation
that can inspect protected business endpoints before applying the classified
probe. Muteki regression selection after the fix: `67 passed`; compileall and
diff-check passed, with only the known pytest cache permission warning.

## Final code convergence checkpoint (2026-08-09)

The remaining duplicated control-plane seams were migrated without changing
Runner internals, Tool Gateway contracts, Evidence schema, or the legacy
Orchestrator:

- IDOR and PATH_TRAVERSAL continuation is owned by the Blackboard-driven
  `MutekiStrategyPlanner`.
- Official-graph route suppression follows the upstream Review proposal and
  Coordinator decision contract, including the upstream failure threshold.
- Native Worker evidence is fail-closed through the existing Evidence
  authority; raw Worker output is not projected into Graph or audit events.
- The frontend subscribes to the full Muteki event family needed for phase,
  Worker, Intent, Fact, Review, branch, lane, and recovery display.

Validation:

- `99 passed, 382 deselected` for the Muteki selection.
- `477 passed, 4 failed` for `backend/tests` from the repository root. The four
  failures are legacy Boolean dispatch tests blocked by unavailable local
  MySQL, not Muteki failures.
- Backend compileall, targeted Ruff, frontend TypeScript/Vite build, and
  `git diff --check` passed.

### Runtime readiness boundary

The canonical local runtime can be selected with:

```text
APP_MUTEKI_GRAPH_BACKEND=upstream
APP_MUTEKI_WORKER_BACKEND=upstream_local
```

Codex CLI authentication is healthy. Container parity is the only remaining
Muteki code/deployment capability: the official
`ghcr.io/fishcodetech/muteki-worker:latest` image is absent, and the earlier
full Ubuntu slim build was blocked by Docker package transport. The temporary
RCP smoke image proved the protocol boundary but is not a substitute for the
official toolchain image. No official image was downloaded in this checkpoint.

## Official container validation (2026-08-10)

The official `ghcr.io/fishcodetech/muteki-worker:latest` image is now
available locally and passed direct Runtime Control Plane startup, handshake,
and teardown checks. The real Asset Warranty service was tested from a Worker
joined to `asset-warranty-net` through `asset-warranty-web:5000`; the published
host port is loopback-only and is not reachable from a sibling container.

The native adapter now injects the projected Codex account, official
Blackboard intent/script variables, and normalized Linux paths for the shared
graph. A fresh container run opened the real upstream graph and created a
durable Evidence chain (AgentTask, ToolCall, Artifact, EvidenceLedger) plus a
safe Blackboard observation. The run ended `COMPLETED_UNSOLVED`: no verified
finding or flag was promoted. This is an honest chain-validation result, not a
solved-target claim. The remaining gap is structured Worker observation
handoff for the next strategy turn.

## Migration audit checkpoint (2026-08-10)

The official Muteki source is vendored in `backend/muteki/`; the remaining
production gap found in this audit was the native Worker-to-Strategy handoff.
The Coordinator previously replaced every successful native Worker product
with a generic observation, so the next Strategy turn could not consume safe
fields such as the action name, tested field, or Boolean-oracle state.

This is now adapted through an official event cursor and incremental
`fact_added` read. A strict allowlist projects bounded fields, filters by the
active Intent and expected tool, and binds the projection to the existing
Evidence references and official `intent_products` relation. Raw Worker/tool
output remains outside the projection.

Validation after the slice:

- focused observation/Worker/Coordinator/UpstreamGraph tests: 28 passed;
- all Muteki tests: 111 passed;
- repository tests: 489 passed, 4 failed in existing legacy Boolean dispatch
  tests blocked by unavailable local MySQL;
- compileall, targeted Ruff, and diff-check: passed.

The next required validation is one fresh bounded multi-step official-container
run against the authorized target network. The previous real run remains
`COMPLETED_UNSOLVED`; no flag or solved target is claimed until the new
structured observation is seen in the next Strategy snapshot.

## Official native protocol validation (2026-08-10)

The native production path now selects the official `codex` engine profile,
inherits the explicitly configured `asset-warranty-net`, and uses the vendored
official `CliSolver` explore protocol. The previous custom one-shot adapter
remains available as the default compatibility protocol for existing tests.

Run `3128405c-7b9c-4f8d-894f-0b04b0eb647b` reached the real target from the
official Worker container. The graph recorded two completed Intent cycles, ten
Fact events, five Review findings, and no verified Flag. The outer Run correctly
ended `COMPLETED_UNSOLVED`; this is a genuine chain result, not a success claim.
The remaining gap is typed observation output: the official explore worker wrote
mostly prose Fact summaries, so Strategy can progress but cannot yet consume a
fully typed action result for the next domain decision.

Focused validation after this slice: 33 passed; backend compileall and targeted
Ruff passed; `git diff --check` passed. The four known repository failures remain
legacy Boolean dispatch tests requiring a reachable local MySQL service.

## Strict native Fact handoff (2026-08-10)

The native boundary now has a strict allowlisted JSON Fact contract. The parser
removes only the official `[engine]`/`VERIFIED_FACT=` envelope, rejects explicit
unverified official events, filters by active Intent and expected tool, and
retains only bounded strategy fields plus Evidence references.

Fresh official-container Run
`8e71b714-a5b4-4461-b027-019a9f6dbe2f` produced a verified typed
`sql_boolean_compare` Fact in the official SQLite graph with an artifact
reference. The same safe Fact shape was consumed by the reusable Strategy unit
path, which selected `oracle_expression_calibration` rather than repeating the
Boolean action. The bounded outer Run still ended `COMPLETED_UNSOLVED` and no
verified flag is claimed; this validates the handoff seam, not the full target
solve.

Focused validation after the slice: 49 tests passed; backend compileall,
targeted Ruff, and `git diff --check` passed. The four known repository failures
remain legacy Boolean tests requiring unavailable local MySQL.

## Typed Fact persistence and bounded progression (2026-08-10)

The native Worker prompt now requires a safe typed JSON observation to be
written through the official Blackboard before the ordinary marker response.
The Strategy adapter accepts the official engine envelope around that safe
Fact, and a failed Boolean field advances to the next declared field rather
than replaying a concluded Intent.

Fresh authorized-target runs:

- `a14f6954-1f9b-4339-ba36-b65be4c37ff1`: verified typed Boolean Fact plus
  artifact reference; final status `COMPLETED_UNSOLVED`.
- `bccea953-9c25-4253-a06d-fa54b93cb580`: typed Boolean result did not verify
  an oracle; final status `COMPLETED_UNSOLVED`.
- `5dc6aea4-2cae-4fe0-aa99-8bdc549a0fc5`: explicit Worker dead-end; the same
  Intent was not silently replayed; final status `COMPLETED_UNSOLVED`.

No flag, verified Finding, or Completion Gate success is claimed. The image,
target network, official graph, Evidence bridge, and cleanup path remain
validated. Focused native/official/strategy tests: `41 passed`; all Muteki
tests: `118 passed`; compileall, targeted Ruff, and diff-check passed.

## Calibration Fact contract (2026-08-10)

The native adapter now has a bounded typed contract for
`oracle_expression_calibration`. Its safe fields are limited to the success
and verification booleans, capability identifiers, extraction strategy, and
request count. The reusable Strategy turns a successful, Evidence-backed
calibration Fact into the next `mysql_metadata_discovery` action.

This contract is unit-validated; no live target run is counted as a successful
calibration because the bounded real runs in this window did not reach that
state. No flag or Completion Gate success is claimed.

Validation after this slice: focused native/official/strategy/upstream-graph
tests `44 passed`; all Muteki tests `121 passed`; compileall and targeted Ruff
passed.

## Review branch tool identity boundary (2026-08-10)

Review-proposed Intents retain `tool_name` only for an exact, small allowlist
of known Solver tools. Unknown identifiers and free-form prose remain advisory
without executable identity. The adapter does not grant authorization,
completion, or Evidence authority.

Validation: focused Review/Fact/Strategy/Worker tests `50 passed`; all Muteki
tests `122 passed`; backend compileall, targeted Ruff, and diff-check passed.
No real target solve or Completion Gate success is claimed.

## Metadata Fact handoff (2026-08-10)

The native adapter now accepts a bounded `mysql_metadata_discovery` product:
database/table/column stage, approved metadata expression, bounded identifier
lists, and request count. Arbitrary SQL, raw responses, and unknown fields do
not cross the seam. Strategy progression from typed database metadata to table
enumeration and from typed table metadata to column enumeration is covered by
Evidence-linked tests.

Validation: focused observation/Worker/Strategy tests `45 passed`; all Muteki
tests `127 passed`; compileall and diff-check passed. Ruff was not available in
the current shell and was not claimed as run. No Completion Gate success is
claimed.

## Calibration-chain live validation (2026-08-10)

Run `59e5488f-12cc-411f-9f8b-fea9a506535f` used the official Worker image and
the authorized target network through the isolated production-style entry.
The upstream graph recorded four proposed Intents, three completed Worker
directions, ten Fact events, four Review findings, and two dead ends. This
confirms the real Coordinator can refresh after external Worker writes and
continue through bounded Review/Intent lifecycle events.

The Worker did not produce a successful typed calibration Fact in this run, so
the run ended `COMPLETED_UNSOLVED`. No verified Finding, flag, or Completion
Gate success is claimed. The temporary service and container were cleaned up;
the existing 8000 service was preserved.

## Official concluded-Intent parity recheck (2026-08-10)

The vendored official implementation was used as the contract for the final
no-replay change. `SQLiteSharedGraph._attempted_intents_block()` surfaces
concluded work to the Reason prompt, while official `dispatch_intents()`
deduplicates active and concluded barren directions. Official Reason does not
invent a new fallback when its next-Intent result is empty.

The local compatibility layer now preserves that behavior for a real Strategy
provider: after all declared Boolean fields have been attempted without a
verified oracle, it returns no next Intent; the provider-aware Reason layer
does not restore the generic SQL fallback. The provider-less compatibility
fallback remains unchanged for isolated graph tests.

Validation:

- Focused Muteki/Reason/Strategy/Worker tests: `72 passed`.
- All Muteki tests with `PYTHONPATH=backend`: `135 passed`.
- Solver runtime regression: `13 passed`.
- `python -m compileall -q backend/app`: passed.
- Targeted Ruff: passed.
- `git diff --check`: passed, with only the existing safe-directory and
  line-ending warnings.

Fresh real chain `4b225ce9-3525-4419-b2f1-6a97105cd2e1`:

- Official graph: 2 proposed/claimed/concluded Intents, 7 Fact events, 3
  Review findings, 1 dead-end.
- Intent sequence: `sql_boolean_compare:asset_no` then
  `sql_boolean_compare:department`; no third generic SQL replay.
- Final status: `COMPLETED_UNSOLVED`.
- Completion Gate: not passed; no verified Oracle, Finding, or Flag was
  promoted.
- Isolation: 8001 and temporary Worker resources stopped; existing 8000
  remains listening.

The target is therefore still unresolved. The next change must again be based
on the official Worker/Graph/Reason contract; no target-specific fallback or
answer inference is authorized.

## Reason verdict compatibility slice (2026-08-10)

The official Reason protocol is now preserved at the local adapter boundary.
The list-compatible projection carries the official `verdict` and `drift`
without changing the existing Intent provider shape. A `course_correct`
verdict is consumed by the compatibility Coordinator as a bounded Review
dispatch through the existing StagePolicy; raw model drift text is not stored
in audit or passed as an executable action.

Validation:

- Focused Reason/Coordinator tests: `10 passed`.
- All Muteki tests: `139 passed`.
- `python -m compileall -q backend/app`: passed.
- Targeted Ruff: passed.
- `git diff --check`: passed with existing line-ending/safe-directory
  warnings.

This slice does not claim a real target solve. The authorized target remains
`COMPLETED_UNSOLVED` pending a typed evidence product accepted by the official
Worker/Graph and the Completion Gate.

## Official Artifact-to-Evidence handoff (2026-08-10)

The native Worker now follows the official Muteki artifact ownership model.
Each Intent gets an isolated official `ArtifactStore`; the adapter returns only
an internal path reference. The existing SQLAlchemy Evidence authority reads
that path, retains the protected artifact, and creates the normal
Artifact/EvidenceLedger chain. Raw output is not projected into Graph facts,
audit events, Worker metadata, or Strategy observations.

Validation:

- Focused official Worker/Observation/Reason/Coordinator tests: `43 passed`.
- All Muteki tests: `140 passed`.
- `python -m compileall -q backend/app`: passed.
- Targeted Ruff: passed.
- `git diff --check`: passed with existing line-ending/safe-directory
  warnings.

The real target remains unresolved and no Completion Gate success is claimed.
The next validation is one isolated official container run using this bridge.

## Artifact-backed real validation (2026-08-10)

Run `8a520769-36a9-4f91-81f6-e59899740ab0` completed through the isolated
production-style entry with the official Worker image and authorized target
network. The run ended `COMPLETED_UNSOLVED` in `REPORTING` with two native
Worker calls and two Evidence Ledger references, but no confirmed fact, flag,
or Completion Gate success. This is recorded as an unsolved run, not a
successful solve.

Safe workspace inspection confirmed per-Intent official artifact files and
existing `evidence/native-workers` files. Raw artifact content was not placed
in this report. The temporary 8001 service was stopped and the existing 8000
service was preserved.

The next source-backed investigation is why the official Worker emitted no
typed verified observation on this real run; the answer must come from the
official Worker/Graph contract, not from target-specific inference.

Final regression after this parity patch: the complete `test_muteki*.py` set
passed `136` tests. This does not change the real target result or authorize a
Completion Gate success.

## Frontend Muteki observability (2026-08-10)

The production workspace already receives the Muteki Graph projection through
the existing durable RunEvent SSE stream. A browser check of Run
`f0594e9f-fdfa-4f97-beb6-d7068bbe46ac` confirmed the Muteki mode label, stage
rail, Blackboard, Intent, Worker, Evidence, Completion and usage surfaces.

The frontend key-state projection was extended to include Worker lifecycle,
Intent claim/release/conclusion, Coordinator directives, Review, and Solver
action/observation events. Only safe event identity, status and reason fields
are displayed; raw Worker output and HTTP response content remain behind the
Evidence boundary.

Validation:

- `npm run build`: passed.
- Browser refresh: 39 Muteki key states rendered, including Worker claim/start,
  Fact write, Coordinator rebootstrap and Review Worker activity.
- This is an observability change only. The authorized target is still
  `COMPLETED_UNSOLVED`; the frontend does not manufacture a Finding or flag.

## A/B validation: shared Blackboard route scheduling (2026-08-13)

Target: 资产保修核验平台 (`challenge_id=04a911ce-6b29-4c5c-9b5f-bbe5b051354b`).
All runs used `solver_mode=muteki`, the official Muteki container Worker path,
and the existing Coordinator Reason model. No challenge source files or target
ground truth were inspected.

| configuration | run | terminal result | elapsed | effective routes | structured endpoint facts / duplicates | Reason start/completed | total tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| Codex only | `451a1899-b764-4813-8f68-7523eff54b26` | TIMEOUT | 945s | 3 | 0 / not emitted by Codex freeform facts | 2 / 2 | 255,700 |
| Codex + one OpenAI | `06f9eeee-f897-4e93-afc6-622b4bdbe008` | TIMEOUT | 946s | 4 | 6 / 0 | 1 / 1 | 19,854 |
| Codex + two OpenAI | `90394007-111d-4cae-b7fe-66da02fd2985` | TIMEOUT | 946s | 6 | 20 / 0 | 1 / 1 | 352,255 |

The empirical sample is one run per configuration: success is therefore
`0/1` for each configuration, not a statistically meaningful success rate.
All three runs completed Prepare -> Race -> Coordinator -> Finalize and were
blocked from claiming a solved result because no verified flag Evidence passed
the Completion Gate. `06f9...` and `9039...` are the post-fix samples; the
earlier `c869...` and `3818...` samples are retained only as pre-fix evidence.

### Production defect found and repaired

The OpenAI-compatible Worker received the native `SQLiteSharedGraph`, whose
authoritative read API is `verified_evidence()`. The adapter initially read
only the compatibility `snapshot()`, so a Worker could not see durable facts
already written by a sibling. The adapter now reads native verified evidence,
rechecks after acquiring the activity lock, and keeps a successful activity
reservation as the Muteki route-level completed marker. The reservation is
released only when the execution does not produce an observation.

This removes both cross-Worker and same-Worker repeated normalized endpoint
requests. A repeated endpoint now returns typed `ROUTE_EXHAUSTED` and becomes a
dead-end/review signal rather than another target request. Codex Worker logic,
Runner, Tool Gateway, Evidence Store schema, and legacy Orchestrator were not
changed.

Validation after the repair:

- Focused Muteki regression suite: `74 passed`.
- Final transport-failure lease correction raised the focused suite result to
  `75 passed`.
- `python -m compileall -q backend/app muteki`: passed.
- Targeted Ruff: passed.
- `git diff --check`: passed; only existing line-ending/safe-directory warnings.
- A unit test confirms a successful route claim remains unavailable to the same
  Worker on its next action.

The target was not solved in this A/B sample. The remaining blocker is solver
coverage/timeout on this target, not loss of Blackboard persistence or endpoint
deduplication.
