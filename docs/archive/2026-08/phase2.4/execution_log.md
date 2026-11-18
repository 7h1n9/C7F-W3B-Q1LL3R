# Phase 2.4 Execution Log

Status: LIVE_VALIDATED_WITH_LIMITS

## 2026-08-08

- Started Phase 2.4 full-chain validation.
- Confirmed repository: `D:\desktop\毕业设计\C7F-W3B-Q1LL3R`.
- Confirmed branch: `refactor-muteki-core`.
- Created Phase 2.4 documentation and test directories.
- Initial audit checkpoint: no real target result had been claimed yet.
- Audited task/run entry, `solver_v2` routing, Blackboard checkpoint
  persistence, RunnerWorker, Tool Gateway, EvidenceLedger, Completion Gate,
  and central EventService.
- Verified the real target homepage and business POST contract with read-only
  HTTP requests. This is reconnaissance evidence only, not a solved result.
- Initial audit checkpoint: the v2 production path did not yet create
  EvidenceLedger rows or call SolverCompletionEvaluator; the implementation
  checkpoint below records that these gaps were closed.

## Implementation and validation checkpoint

- Added the production `GatewayWorker` adapter and wired Solver v2 through the existing Tool Gateway/Runner boundary.
- Added Solver Evidence authority projection and Completion Gate evaluation. A verified Finding with valid Evidence references is required; database/table/schema facts and ordinary configuration values are rejected.
- Added Solver answer materialization only after the challenge flag pattern matches, plus safe fresh-reproduction provenance.
- Added report normalization so solved Solver v2 runs report no active blocker while retaining failed-tool history.
- Added bounded retry state for transient script errors. The retry is explicit in Blackboard control and never silently replays an unfinished action.
- Added `backend/tests/phase24/` with runtime chain, tool failure recovery, timeout, Completion Gate, and Evidence validation tests.

### Real target sample

Target: `http://192.168.236.1:28656/`

Measured sample: 13 runs, 11 solved and 2 controlled unsolved under concurrent load. The two unsolved runs were `f35fad5f-e437-4fa3-9007-610c8020e123` and `fd7fc60e-cff3-44a6-a5ad-47e36d1ed613`; both stopped with `SOLVER_NO_ACTION` after a script returned only 7 requests and 1 transient error. The sequential recovery run `da0da023-8a0d-41fb-9bb9-6b8d2f76b5ac` and final post-validation run `b69fad11-b97a-4ff3-9351-7a9e1844d1c1` both solved and passed fresh reproduction.

## Final post-validation checkpoint

- Run ID: `b69fad11-b97a-4ff3-9351-7a9e1844d1c1`
- API path: `POST /api/v1/challenges/{challenge_id}/runs` followed by
  `POST /api/v1/runs/{run_id}/start`
- Solver mode: `solver_v2`
- Target: `http://192.168.236.1:28656/`
- Result: `COMPLETED_SOLVED`, 30 Tool Calls, Reporting phase
- Audit: solver start/action/tool/observation/completion events persisted
- Fresh reproduction: `executed=true`, `verified=true`,
  `fresh_session=true`, `fresh_flag_artifact=true`
- Completion: verified Finding and Evidence references; no blocker

## Log format

Each entry must include:

- timestamp
- run ID
- command or API path
- code revision
- target URL
- observed lifecycle/events
- evidence references
- completion decision
- failure and recovery result

## 2026-08-09 — Muteki live validation

- Run ID: `234d17a5-0c48-4cd0-94ea-11109607706e`.
- API: create Run with `solver_mode=muteki`, then start it through the public
  Run API.
- Target: `http://192.168.236.1:28656/`.
- Result: `COMPLETED_SOLVED`; 16 completed ToolCalls; 16 Evidence refs.
- Events: phase changes for `prepare`, `race`, `coordinator`, `finalize`,
  plus `muteki.run_finished` and outer `run.completed`.
- Frontend: browser workspace displayed terminal `FINALIZE`, Muteki facts,
  Worker/tool timeline and audit events through SSE.
- Validation: 40 focused backend tests passed; compileall, Ruff and diff
  checks passed; frontend `npm run build` passed; backend, bridge, frontend,
  Runner and host engine health checks passed where applicable.
- Remaining: ten-run pressure and container model execution are pending.

## 2026-08-09 - Historical document preview center

- Challenge: `f773ad47-b80d-49e3-8254-9e0dfe11c582`.
- Target: `http://192.168.236.1:28252`.
- Initial bounded runs showed that the scanner stopped at the login page or
  failed to claim the generated preview intent. The scanner now follows a
  bounded second link level, Race persists disclosed archive paths, and the
  Coordinator claims intents before worker spawn.
- The final bounded path was classified as `PATH_TRAVERSAL` with confidence
  88. The successful preview-path variant was executed through the existing
  Tool Gateway and Runner adapter; no target answer is stored in this log.
- Final validation Run: `076a0026-043c-493a-bedc-179c799999bb`.
- Result: `COMPLETED_SOLVED`; 36 public Run tool calls, 40 canonical graph
  facts, 3 completed preview-path intents, 1 verified graph flag, and 18
  Evidence references.
- Formal report validation: `/api/v1/runs/{run_id}/report` returned a solved
  report with `flag_verified=true` and 18 Evidence references. Added a
  Muteki-only report materializer for the existing report artifact contract;
  legacy report generation remains unchanged.
- Regression: runtime integration 20 passed; Muteki/recon/coordinator/adapter
  selection 21 passed; compileall, target Ruff, and diff-check passed.
- The run is a real target success. Ten-run pressure and proprietary CLI
  container execution are not claimed.

## 2026-08-09 - Blackboard-driven strategy expansion

- Root cause confirmed: the production Muteki Reason provider previously had
  only SQLI, IDOR, and PATH_TRAVERSAL branches. Other classifications could
  finish after Race with no next intent. The SQL gate also rejected confirmed
  SQLI tools, and worker exceptions were not always represented as graph dead
  ends.
- Added a deterministic `MutekiStrategyPlanner` driven only by graph facts and
  challenge metadata. It adds bounded non-SQL web plans, complete asset SQL
  contracts, explicit field fallback, calibration matrix, extraction profile
  propagation, metadata stage ordering, and no-action stopping after an
  exhausted route.
- Added sanitized semantic result projection and bounded worker failure facts;
  response bodies, cookies, tokens, and secrets do not enter the Blackboard.
- Hardened Muteki Run cleanup by reloading Run/Attempt/Lease after Gateway
  commits or failures; this removed the secondary async ORM `MissingGreenlet`
  failure during lifecycle accounting.

### Controlled live results

- Asset Warranty challenge `2e9ba4c4-17d3-4b77-97bd-40a331e6fd8b`, target
  `http://192.168.236.1:28346/`: Runs `34419318-321a-4487-87c0-da31e8392b36`,
  `903bc65b-833d-4f14-9fe8-eda5fe52035f`, and `7b6b6a29-ab8b-4622-bcb8-646dc90e1cd0`
  reached Boolean field selection, calibration, and MySQL metadata stages but
  ended `COMPLETED_UNSOLVED` because the target returned no distinguishable
  `DATABASE()`/`information_schema.tables` metadata fact. No Finding was
  fabricated and the Completion Gate correctly refused SOLVED.
- The asset chain confirmed `asset_no` was not an oracle while `department`
  was a stable TRUE/FALSE oracle; calibration now uses the confirmed field.
- Device Warranty challenge `851d3a3e-e3a8-4c17-8f51-caa2008ee48d`, fresh Run
  `e0c105e1-d686-48e2-8ae3-c76c1719d518`, target `http://192.168.236.1:28656`:
  `COMPLETED_SOLVED`, 32 ToolCalls, all `http_session_request`. This confirms
  the existing IDOR strategy remains compatible with the new strategy layer.
- One API start request returned `STARTING` while no background task
  progressed; direct invocation of the same production `RunSupervisor`
  completed the Run. This is a service startup observation, separate from
  Solver logic.

### Lifecycle reproduction

- Run ID: `68b41c1a-69fd-42ed-89d5-bdf8b165ee29`.
- Result: `COMPLETED_SOLVED`; outer `current_phase=REPORTING`.
- Report: `/api/v1/runs/{run_id}/report` returned `solved`,
  `flag_verified=true`, and 18 Evidence references.
- This reproduction verified the explicit Muteki terminal phase mapping added
  in `RunSupervisor`; the initial `INTAKE` phase is no longer retained after
  a solved Muteki run.

## 2026-08-09 - Upstream graph fresh-run validation

- A feature-flagged temporary backend on port `8002` used
  `APP_MUTEKI_GRAPH_BACKEND=upstream`; the default production backend on port
  `8000` was not changed.
- Historical Document Preview Center run
  `81e3a96e-6847-443b-8b5a-457c144eab1a` reached the Tool Gateway but the
  target `192.168.236.1:28252` returned an empty reply. The run was cancelled
  with `TARGET_UNAVAILABLE`; no solver success is claimed.
- Reachable Email Template Preview System first run
  `b4465c09-52c5-4fd6-8621-5d0afff9bcb4` completed unsolved after 13 bounded
  session calls. It proved upstream Graph persistence, Evidence references,
  event projection, report generation, and SSE delivery.
- The run exposed a false IDOR classification caused by treating a 404
  `/tickets` probe as an authenticated object surface. The classifier now
  requires the candidate endpoint itself to be protected or successful, and
  Race now extracts explicitly published code-pair credentials such as
  `employee / employee-pass`.
- Re-run `a69d9f6d-a955-40d4-b536-64eb5fe5690d` completed unsolved with
  classification `SSTI`, 22 Tool Calls, no SQL tool calls, and a complete
  canonical Graph/Evidence/SSE trail. The next strategy slice is an
  authenticated session continuation for non-IDOR business surfaces.

## 2026-08-09 - Muteki migration convergence

- Moved the reusable IDOR and PATH_TRAVERSAL continuation decisions into
  `MutekiStrategyPlanner`; `MutekiRuntime` now keeps only a narrow low-confidence
  compatibility fallback for incomplete historical fact snapshots.
- Migrated official-graph review suppression to the upstream two-step contract:
  `ReviewWorker` emits a tier-2 `ROUTE_SUPPRESS` proposal and the Coordinator
  applies it only after the official failure/confidence threshold is met. The
  current compatibility graph behavior remains unchanged.
- The native Worker path is complete behind the existing production adapters:
  local mode uses the authenticated Codex CLI, container mode uses the vendored
  upstream `container_exec` Runtime Control Plane, and successful native output
  must pass the existing Evidence authority before it can update the Graph.
- Frontend SSE subscriptions now include the Muteki worker, review, branch,
  lane, fact, route, directive, HITL, intent, and graph-compaction event types;
  the production frontend build succeeds.

### Validation checkpoint

- Muteki regression selection: `99 passed, 382 deselected`.
- Backend tests collected from the repository root: `477 passed, 4 failed`.
  The four failures are pre-existing legacy Boolean dispatch checks that need a
  reachable local MySQL service (`asyncmy` cannot connect to `localhost`); no
  Muteki test failed.
- `python -m compileall -q backend/app`: passed.
- Targeted Ruff for `backend/app/solver/muteki` and related tests: passed.
- `frontend/npm run build`: passed; only the existing Vite large-chunk warning
  remains.
- `git diff --check`: passed; Git emitted only existing safe-directory and
  line-ending warnings.

### Remaining environment boundary

- `codex --version` reports `0.147.0-alpha.6.5` and `codex login status` reports
  ChatGPT authentication, so the local native Worker path is runnable.
- `APP_MUTEKI_GRAPH_BACKEND=upstream` plus
  `APP_MUTEKI_WORKER_BACKEND=upstream_local` selects the canonical vendored
  Graph/Coordinator/Worker path without a container image.
- The official container image
  `ghcr.io/fishcodetech/muteki-worker:latest` is not present locally. The full
  official image build previously failed while downloading Ubuntu package
  archives through the configured Docker proxy. No new image download/build
  was started in this checkpoint, per the user's deferral.

## 2026-08-10 - Official Worker image and container chain

- Pulled and verified the official `ghcr.io/fishcodetech/muteki-worker:latest`
  image: linux/amd64, local image ID
  `sha256:438f5029bd6a8a4363bcfd32ded833aa3041075786ec470f530b8259a6b646e0`,
  size 7,800,329,359 bytes.
- Direct image and official RCP startup/handshake/teardown checks passed.
- The Asset Warranty service was reachable from the Worker through its
  authorized Docker network alias `asset-warranty-web:5000`. The published
  `192.168.236.1:28346` binding is host-loopback-only and was not used from
  inside the sibling container.
- Fixed the native Worker boundary to project credentials, inject
  `MUTEKI_INTENT_ID` and `MUTEKI_BLACKBOARD_SCRIPT`, and normalize Windows
  host paths to Linux container paths. A fresh run no longer creates the
  erroneous `graph\\...` file and the Worker opens the real upstream graph.
- Real container runs `1358f0ff-b8e2-4034-8f15-9f07b3c17a26` and
  `683da48a-3b3e-4527-9c3e-627137dd4c89` reached the real target and created
  Evidence/Artifact/ToolCall/AgentTask plus a safe generic Blackboard fact;
  both correctly ended `COMPLETED_UNSOLVED` without a verified finding.
- Single-step run `683da48a-3b3e-4527-9c3e-627137dd4c89` confirmed the final
  path fix. No solved flag is claimed. The remaining production gap is a
  structured native observation handoff so the next Strategy action can be
  selected from the Worker result.
- Validation after the adapter changes: 106 Muteki tests passed, official
  Worker/Coordinator tests 20 passed, compileall/Ruff/diff-check passed.

## 2026-08-10 - Native Worker observation handoff

- Audited the cloned official source against the vendored `backend/muteki/`
  tree; the Python source is already present. The active migration gap was
  production consumption of official `fact_added` products, not source
  availability.
- Added a strict observation projection at the Coordinator boundary. It reads
  only new official fact events after a per-action cursor, filters by Intent
  and expected tool, and retains bounded strategy fields plus existing
  Evidence references.
- Added official `intent_products` linkage for the Coordinator projection
  without changing the database schema. Unstructured Worker facts retain the
  prior generic safe fallback.
- No raw Worker output, HTTP response, cookie, token, secret, or flag enters
  the projection or audit state.
- Validation: focused tests 28 passed; all Muteki tests 111 passed; compileall,
  targeted Ruff, and diff-check passed. Repository tests were 489 passed and 4
  failed in legacy Boolean dispatch tests requiring unavailable local MySQL.
- A fresh real multi-step container run after this change is pending; the last
  real run before it ended `COMPLETED_UNSOLVED` and no solved target is claimed.

## 2026-08-10 - Official native CliSolver integration

- Fixed native upstream profile selection: upstream local/container Workers now
  receive the official `codex` profile; the compatibility Gateway path keeps
  `gateway-runner`.
- Fixed native container network inheritance: upstream Workers honor the
  explicitly configured `MUTEKI_CONTAINER_NETWORK` and keep `bridge` as the
  default for callers that do not opt into a target network.
- Added an explicit `OfficialWorkerConfig.protocol` boundary. The default
  remains the tested compatibility `one_shot` protocol; native upstream
  production uses the vendored official `muteki.solver.cli_solver.CliSolver`
  in bounded `explore` mode.
- The native adapter receives the official SharedGraph and leaves Intent
  claim/conclusion, Fact/DeadEnd/Flag provenance, and the upstream gate in the
  official solver. Only a bounded status summary crosses the existing Evidence
  bridge.
- Fresh container Run `3128405c-7b9c-4f8d-894f-0b04b0eb647b` reached the real
  Asset Warranty target through `asset-warranty-net`, completed two official
  Intent cycles, recorded ten Fact events and five Review findings, and cleaned
  up its Worker container. It ended `COMPLETED_UNSOLVED` with no verified Flag.
- This proves official `CliSolver explore -> SharedGraph -> Coordinator`
  progression. Most official facts were prose summaries rather than the strict
  JSON observation contract, so the next Strategy action still needs a stronger
  typed handoff or an explicitly selected structured-fact protocol.
- Validation after this slice: 33 focused Muteki/official tests passed;
  compileall, targeted Ruff, and diff-check passed.

## 2026-08-10 - Strict native Fact handoff validation

- Added a strict official Fact envelope parser. It accepts only JSON facts with
  bounded tool/result fields, strips the official engine/marker envelope, and
  rejects explicit unverified events before they reach Strategy.
- Added a typed handoff contract to native `CliSolver` and compatibility
  one-shot prompts. The Worker must execute the bounded action, then emit a
  safe observation JSON witness followed by `VERIFIED_FACT`; raw response,
  credentials, cookies, tokens, secrets, and flags are excluded.
- Fresh official-container Run
  `8e71b714-a5b4-4461-b027-019a9f6dbe2f` used the official SQLite graph schema
  and produced a verified typed `sql_boolean_compare` observation with an
  artifact reference. No raw Fact content was copied into this log.
- The bounded Run ended `COMPLETED_UNSOLVED` with no verified flag. The
  Strategy unit path consumed the typed JSON observation and selected
  `oracle_expression_calibration` with the evidence reference; the bounded
  outer Run did not claim a solved target.
- Focused validation: 49 passed; compileall, targeted Ruff, and diff-check
  passed. The isolated 8001 service and `muteki-run-*` containers were stopped;
  existing project containers were preserved.

## 2026-08-10 - Typed Fact persistence and failed-field progression

- The official Worker prompt now requires the same allowlisted JSON observation
  to be persisted through the official Blackboard `write-fact --verified`
  command before the marker reply. This keeps the machine-readable handoff in
  the upstream graph instead of relying on prose `VERIFIED_FACT` summaries.
- Strategy now accepts the official `[engine]` envelope around a safe typed Fact
  through the existing observation parser. It also advances to the next
  declared SQL field after a verified failed Boolean attempt, while preserving
  route de-duplication and fail-closed stop behavior.
- Fresh Run `a14f6954-1f9b-4339-ba36-b65be4c37ff1` produced a verified typed
  `sql_boolean_compare` Fact with an artifact reference and ended
  `COMPLETED_UNSOLVED`; no verified flag was promoted.
- Fresh Run `bccea953-9c25-4253-a06d-fa54b93cb580` also reached the official
  Worker and recorded a typed Boolean result, but the bounded result did not
  establish a verified oracle. Fresh Run `5dc6aea4-2cae-4fe0-aa99-8bdc549a0fc5`
  recorded an explicit dead-end and stopped without replaying the same Intent.
- These runs validate safe Fact persistence and bounded recovery/stop semantics,
  not full target completion. The authorized target still has no Completion Gate
  success in this validation window.
- Validation: focused native/official/strategy tests `41 passed`; all Muteki
  tests `118 passed`; backend compileall, targeted Ruff, and `git diff --check`
  passed. The isolated 8001 service and all `muteki-run-*` containers were
  stopped; the existing 8000 service was preserved.

## 2026-08-10 - Calibration Fact contract

- Added a strict typed handoff shape for `oracle_expression_calibration`.
  Only `success`, `oracle_verified`, bounded capability identifiers,
  `extraction_strategy`, and `request_count` are accepted at this seam.
- Strategy projects `extraction_strategy` into its existing bounded extraction
  profile and continues to `mysql_metadata_discovery` only after the
  calibration Fact is successful and Evidence-backed.
- No real target run was claimed for this branch because the bounded runs in
  this window did not produce a successful calibration action. The contract is
  covered by unit and adapter tests before the next live run.
- Validation: focused native/official/strategy/upstream-graph tests `44 passed`;
  all Muteki tests `121 passed`; backend compileall and targeted Ruff passed.

## 2026-08-10 - Review branch tool identity boundary

- Review-proposed Intents now preserve `tool_name` only when the proposal
  contains one exact identifier from the small Solver action allowlist.
- Unknown or free-form Review prose remains advisory and does not acquire an
  executable tool identity. Review still cannot authorize execution or declare
  success.
- Validation after this slice: focused Review/Fact/Strategy/Worker tests `50
  passed`; all Muteki tests `122 passed`; backend compileall, targeted Ruff,
  and diff-check passed.

## 2026-08-10 - Calibration-chain bounded live validation

- Fresh Run `59e5488f-12cc-411f-9f8b-fea9a506535f` used the official Worker
  image and `asset-warranty-net` through the isolated 8001 service.
- The official graph recorded four proposed Intents, three completed Worker
  directions, ten Fact events, four Review findings, and two dead ends. The
  cross-process Coordinator revision path and bounded no-replay behavior were
  exercised in the real container chain.
- This run did not produce a successful typed calibration Fact; the native
  Worker directions ended in bounded dead-end/uncertainty paths. The outer Run
  correctly ended `COMPLETED_UNSOLVED`, with no verified Finding, Flag, or
  Completion Gate success claimed.
- The temporary 8001 service and Worker container were stopped afterward;
  existing 8000 remains untouched.

## 2026-08-10 - Metadata Fact handoff

- Added a strict `mysql_metadata_discovery` handoff for database, table, and
  column products. Only the stages `database`, `tables`, and `columns`, the
  approved metadata expressions, bounded identifiers/lists, and request count
  cross the native observation seam.
- Arbitrary SQL expressions, raw responses, and unlisted fields remain
  rejected. Strategy tests now prove typed database metadata progresses to
  tables and typed table metadata progresses to columns with Evidence refs.
- Validation: focused observation/Worker/Strategy tests `45 passed`; all
  Muteki tests `127 passed`; backend compileall and diff-check passed. Ruff is
  not installed in the current shell, so no new Ruff invocation was claimed.
- No real target run was counted as solved; the latest authorized target
  result remains `COMPLETED_UNSOLVED`.

## 2026-08-10 - Official concluded-Intent parity and bounded live recheck

- Compared the adapter behavior with the vendored official Muteki source:
  `backend/muteki/swarm/shared_graph.py` keeps concluded Intents and exposes
  them to Reason through `_attempted_intents_block()`;
  `backend/muteki/solver/reason.py` returns an empty Intent set when the
  Reason result is exhausted and `dispatch_intents()` suppresses concluded
  barren directions. It does not recreate a generic SQL fallback.
- The adapter had one parity gap: after both declared Boolean fields were
  attempted without a verified oracle, the Strategy returned no route but the
  compatibility Reason layer could restore a generic `sql_boolean_compare`.
- Minimal parity fix: an exhausted Strategy route now remains empty, and the
  Reason fallback is retained only for provider-less compatibility tests. A
  real provider returning no next Intent no longer resurrects the generic
  fallback.
- Added one regression test for the exhausted declared-field route and one
  Reason gate test.
- Focused validation: `72 passed`; all Muteki tests: `135 passed`; runtime
  regression: `13 passed`; compileall, targeted Ruff, and `git diff --check`
  passed.
- Fresh authorized-target Run `4b225ce9-3525-4419-b2f1-6a97105cd2e1` used the
  official Worker image and target network through isolated port 8001. The
  official graph recorded exactly two proposed/claimed/concluded SQL Intents
  (`asset_no` and `department`), seven Fact events, three Review findings,
  and one dead-end. No generic third SQL Intent was proposed.
- The Run ended `COMPLETED_UNSOLVED` with no verified Oracle, Finding, Flag, or
  Completion Gate success. The existing 8000 service was preserved; isolated
  8001 and Worker resources were stopped afterward.
- This is a Muteki semantic parity and no-replay validation, not a claim that
  the authorized target has been solved.

## 2026-08-10 - Final regression after parity patch

- Re-ran the complete `test_muteki*.py` set after the Reason change: `136
  passed`.
- The prior focused and runtime results remain valid: focused `72 passed`,
  runtime regression `13 passed`, compileall, targeted Ruff, and diff-check
  passed.

## 2026-08-10 - Official Reason verdict handoff

- Compared the vendored official `run_reason()` contract with the local
  `upstream_reason` adapter. The official result carries `verdict` and
  `drift` in addition to Intent proposals; the adapter previously dropped
  both fields.
- Added a list-compatible `UpstreamReasonProposals` envelope so existing
  provider callers remain compatible while `MutekiReason` preserves the
  official `course_correct`/`complete` decision fields.
- The compatibility Coordinator now consumes `course_correct` as a bounded
  scheduling decision: it emits a safe coordinator directive and routes one
  existing Review Worker through StagePolicy. Drift prose is not copied into
  audit or Worker payloads.
- Added regression coverage for envelope preservation and course-correction
  Review dispatch. All Muteki tests now pass `139` tests; compileall, targeted
  Ruff, and diff-check pass.
- No real target run was started for this protocol-only slice. The latest
  authorized target result remains `COMPLETED_UNSOLVED`; no Finding, flag, or
  Completion Gate success is claimed.

## 2026-08-10 - Official Artifact-to-Evidence handoff

- Compared the official `ArtifactStore`/`CliSolver` contract with the native
  Evidence adapter. The prior bridge retained only a compatibility summary,
  so the existing Evidence authority had no protected copy of the official
  Worker transcript.
- Native `CliSolver` now writes to a per-Intent official artifact store under
  the run workspace and passes only the host-local artifact path internally.
  `SqlAlchemyOfficialEvidenceBridge` reads that path inside the Evidence
  boundary and creates the existing Artifact/EvidenceLedger chain. No raw
  output enters Graph facts, audit events, Worker metadata, or strategy state.
- Compatibility/legacy Workers still use their bounded `output` field when no
  official artifact path exists.
- Focused official Worker/Observation/Reason/Coordinator tests: `43 passed`.
- Complete Muteki test set: `140 passed`; compileall, targeted Ruff, and
  diff-check passed.
- No real target run was started after this Evidence-only change. The latest
  authorized target remains `COMPLETED_UNSOLVED`.

## 2026-08-10 - Artifact-backed real Run

- Started the isolated 8001 production entry from `backend` so the existing
  MySQL environment file was loaded. The first root-directory startup attempt
  failed at application startup against default `localhost` MySQL and was not
  treated as a code regression.
- Authorized Run `8a520769-36a9-4f91-81f6-e59899740ab0` then ran with
  `solver_mode=muteki`, official Graph/Worker image, and `asset-warranty-net`.
- Safe final result: `COMPLETED_UNSOLVED`, `REPORTING`, two completed native
  Worker calls, two Evidence Ledger references, zero confirmed facts, and no
  Completion Gate success. No flag was inferred.
- The run workspace contains per-Intent official `.muteki-artifacts` and
  existing `evidence/native-workers` artifacts. Only safe filenames and sizes
  were inspected; raw content was not copied into this report.
- Stopped isolated 8001 and preserved the existing 8000 service. No temporary
  `muteki-run-*` Worker container remains.

## 2026-08-10 - Muteki UI event visibility slice

- Browser-verified the real Muteki Run `f0594e9f-fdfa-4f97-beb6-d7068bbe46ac`
  through the existing frontend workspace. The Run is rendered as
  `MUTEKI / SOLVER LOOP` with `Prepare -> Race -> Coordinator -> Finalize`,
  Blackboard, Intent, Worker, Evidence, Completion and token summary surfaces.
- Extended the key-state projection so the main timeline also shows official
  Worker lifecycle, Intent claim/release/conclusion, Coordinator rebootstrap,
  Review, and Solver action/observation events. The projection uses only safe
  identity/status/reason fields; it does not render raw Worker output or HTTP
  response data.
- Browser verification showed the main timeline increasing from 25 to 39 key
  states and visibly included `Worker 认领 Intent`, `Worker 启动`, `黑板写入事实`,
  `Coordinator 调度`, and `Review 发现`.
- Frontend production build passed. This improves observability only; the
  authorized target remains `COMPLETED_UNSOLVED` and no solve is claimed.

## 2026-08-13 - A/B validation and route dedup repair

- Validated the same asset-warranty challenge with Codex only (`451a...`),
  Codex plus one OpenAI-compatible Worker (`06f9...`), and Codex plus two
  OpenAI-compatible Workers (`9039...`). Each terminal run reached timeout
  after Prepare/Race/Coordinator/Finalize; none passed Completion Gate.
- The first post-change production observation showed that native Graph facts
  were not visible to OpenAI-compatible Workers because the adapter read only
  the compatibility snapshot. The fix reads native `verified_evidence()`.
- A second same-run issue was found: successful HTTP activity locks were
  released immediately, allowing repeated actions within one model turn. The
  fix retains the successful route reservation and releases only unsuccessful
  executions. New test: same Worker cannot execute a successful endpoint twice.
- Post-fix durable traces show normalized endpoint duplication `0` in both
  multi-engine samples. OpenAI routes emit `ROUTE_EXHAUSTED` when no new route
  remains. Coordinator Reason completed successfully in both multi-engine
  runs.
- Focused tests before the final transport-failure lease refinement: `74
  passed`; after the refinement: `75 passed`. Compileall, targeted Ruff, and
  diff-check passed.
- The two-openai run `a54...` was canceled during diagnosis and is excluded
  from the A/B table. No target source or ground truth was read.
