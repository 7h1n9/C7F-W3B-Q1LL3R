export interface Challenge { id: string; name: string; description: string; challenge_type: "WEB_TARGET" | "TRAFFIC_ANALYSIS"; target_url?: string | null; allowed_hosts: string[]; flag_pattern: string; source_path?: string | null; status: string; primary_attachment_id?: string | null; metadata_json?: Record<string, unknown>; run_count?: number; solved_run_count?: number; latest_run_status?: string | null; latest_run_started_at?: string | null; latest_run_engine_type?: string | null; created_at: string; updated_at: string }
export interface Skill { id: string; name: string; display_name: string; description: string; source_type: "BUILTIN" | "CUSTOM"; skill_kind: "CORE" | "METHODOLOGY" | "SPECIALIST"; activation_mode: "ALWAYS" | "AUTO" | "MANUAL"; triggers: string[]; negative_triggers?: string[]; prerequisites: string[]; required_tools: string[]; recommended_tools: string[]; forbidden_tools: string[]; ctf_phases: string[]; challenge_types: string[]; content_markdown: string; allowed_tools: string[]; risk_level: string; version: number; enabled: boolean; catalog_scope?: string; binding_count: number; created_at: string; updated_at: string }
export interface ChallengeConversation { id: string; challenge_id: string; model_config_id?: string | null; title: string; status: string; created_at: string; updated_at: string }
export interface ChallengeMessage { id: string; conversation_id: string; role: "user" | "assistant" | "system"; content: string; status: string; error_message?: string | null; created_at: string }
export type SolverMode = "single_agent" | "multi_agent_v1" | "solver_v2" | "muteki";
export type KnowledgeMode = "off" | "on";
// Run-time Worker engine vocabulary. Mirrors WORKER_ENGINE_TYPES in the backend
// (app/schemas/model_config.py). "local_proxy" is NOT here on purpose: it is a
// provider that executes through the openai_compatible engine.
// cursor_cli is retired as a Worker engine (see RETIRED_PROVIDER_TYPES on the
// backend), so it is no longer selectable for a new Run.
export type WorkerEngineType = "openai_compatible" | "codex_cli" | "claude_cli";
// Stored provider_type. Mirrors ProviderType in the backend. It is a superset of
// WorkerEngineType: local_proxy maps onto the openai_compatible engine.
// cursor_cli stays in this union on purpose: configurations created before the
// channel was retired are still returned by the API and must keep rendering.
export type ModelProviderType = "openai_compatible" | "local_proxy" | "codex_cli" | "claude_cli" | "cursor_cli";
export interface WorkerEngineSelection { engine_type: WorkerEngineType; engine_id?: string; model_config_id?: string | null }
export interface ModelConfig { id: string; name: string; provider_type: ModelProviderType | string; runtime_engine_type?: string; base_url?: string | null; wire_api?: "responses" | "chat_completions" | null; model_name?: string; reasoning_effort?: "none" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra" | null; enabled: boolean; api_key_configured: boolean; roles?: Array<"worker" | "coordinator_reason">; action_protocol?: string; structured_output_mode?: string; request_timeout_seconds?: number; max_output_tokens?: number; temperature?: number; max_retries?: number; retry_base_seconds?: number; rate_limit_cooldown_seconds?: number; requests_per_minute?: number; max_concurrency?: number; context_token_limit?: number; route_via_proxy?: boolean; model_aliases?: Record<string, string>; capabilities?: Record<string, unknown>; last_test_at?: string | null; last_test_ok?: boolean | null }
export interface SolveRun { id: string; challenge_id: string; challenge_name?: string | null; title?: string | null; challenge_type?: string | null; target_summary?: string | null; engine_type: string; solver_mode: SolverMode; model_config_id?: string | null; model_name?: string | null; reason_model_config_id?: string | null; reason_model_name?: string | null; worker_engines?: WorkerEngineSelection[]; role_name?: string | null; role_version?: string | null; role_snapshot_json: Record<string, unknown>; hints_json?: Record<string, unknown>; status: string; current_phase: string; workspace_path: string; max_agent_steps: number; max_tool_calls: number; max_context_observations: number; max_runtime_seconds: number; max_total_runtime_seconds?: number; agent_checkpoint_interval?: number; context_revision?: number; infrastructure_retry_count?: number; agent_step_count: number; tool_call_count: number; run_total_agent_steps?: number; run_total_logical_tool_calls?: number; attempt_agent_steps?: number; attempt_logical_tool_calls?: number; checkpoint_segment_steps?: number; current_attempt_number?: number; last_error_code?: string | null; last_error_message?: string | null; active_skill_names?: string[]; diagnostic_tags?: string[]; diagnostic_summary?: string | null; started_at?: string | null; finished_at?: string | null; created_at: string; updated_at: string }
export interface RunControlExperimentRequest {
  factor: "knowledge_mode";
  counterfactual_value: KnowledgeMode;
  start: true;
}
export interface RunControlExperimentResponse {
  experiment_id: string;
  source_run: SolveRun;
  control_run: SolveRun;
  started_run_ids: string[];
  start_error?: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  } | null;
}
export interface FlagCandidate { id: string; candidate: string; verified: boolean; review_state: "OPEN" | "VALID" | "INVALID"; pattern_matched: boolean }
export interface RunEvent { id: string; run_id: string; sequence: number; event_type: string; payload_json: Record<string, unknown>; created_at: string }
export interface RunUsageRow { model: string; role: string; source: string; calls: number; input_tokens: number; output_tokens: number; total_tokens: number; cost_usd: number }
export interface RunUsage { total: RunUsageRow; models: RunUsageRow[] }
export interface MutekiBoardSemantic { status: "idle" | "running" | "completed" | "failed"; model?: string; revision?: number; fingerprint?: string; items?: Array<{ card_id: string; summary_zh: string; category: string; importance: "high" | "medium" | "low" }>; usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number }; error_code?: string; updated_at?: string }
export interface MutekiGraphState { available: boolean; run_id: string; revision: number; facts: Array<{ sequence: number; content: string; verified: boolean; evidence_refs: string[]; source_worker_id?: string; created_at?: string; state?: string }>; key_conditions?: Array<{ sequence: number; content: string; summary_zh: string; verified: boolean; evidence_refs: string[]; confidence?: number; source_worker_id?: string }>; board_semantic?: MutekiBoardSemantic; intents: Array<{ id: string; description: string; status: "open" | "claimed" | "done"; worker?: string | null; result?: string; sequence?: number }>; dead_ends: Array<{ sequence: number; description: string }>; flags: Array<{ sequence: number; flag: string; verified: boolean; evidence_refs: string[] }>; pocs?: Array<{ poc_id: string; name?: string; path?: string; entry_command?: string; status?: string; note?: string; artifact_id?: string }>; reason?: string }
export interface RunUserInput { id: string; content: string; input_type: string; status: string; revision: number; created_at: string; consumed_at?: string | null }
export interface SolverState { id: string; run_id: string; current_phase: string; confirmed_facts_json: Record<string, unknown>[]; rejected_paths_json: Record<string, unknown>[]; active_hypotheses_json: Record<string, unknown>[]; action_fingerprints_json: Record<string, unknown>; active_skill_ids_json: string[]; skill_recommendations_json: Record<string, unknown>[]; run_plan_json?: Record<string, unknown>; capability_ledger_json?: Record<string, unknown>; read_files_json?: string[]; read_ranges_json?: Record<string, unknown>[]; last_decision_card_json?: Record<string, unknown>; last_experiment_json?: Record<string, unknown>; no_progress_count: number; last_progress_at?: string | null; created_at: string; updated_at: string }
export interface RunDiagnosticAnomaly { code: string; severity: string; title: string; summary: string; evidence: string[]; suggestion: string }
export interface RunDiagnostics { diagnostic_tags: string[]; diagnostic_summary?: string | null; anomalies: RunDiagnosticAnomaly[]; state: { current_phase: string; no_progress_count: number; active_skill_names: string[]; recommended_skills: Record<string, unknown>[] }; challenge: { name?: string | null; challenge_type?: string | null; target_summary?: string | null }; engine: { engine_type: string; model_name?: string | null } }
export interface RunHealth {
  status: string;
  current_phase: string;
  last_error_code?: string | null;
  runtime: { active_lease: boolean; lease_owner?: string | null; running_task: boolean; running_tool: boolean };
  input: { queued: number; consumed: number };
  progress: { last_fact?: string | null; last_tool?: string | null; last_tool_status?: string | null; no_progress_count: number };
  next_action: string;
  checkpoint: Record<string, unknown>;
}
export interface KnowledgeProvenance { source_type: string; source_ref: string; source_url?: string | null; notes?: string | null }
export interface KnowledgeItem {
  id: string;
  kind: "technique" | "case" | "poc" | "negative";
  title: string;
  technique: string;
  summary: string;
  preconditions: string[];
  target_stack: string[];
  payload_template?: string | null;
  verification_method: string[];
  known_failures: string[];
  steps: string[];
  provenance: KnowledgeProvenance;
  published_at?: string | null;
  trust_level: "reviewed" | "curated" | "unverified";
}
export interface KnowledgeListResponse { items: KnowledgeItem[]; total: number; limit: number; offset: number }
export interface KnowledgeStats {
  total: number;
  by_kind: Record<string, number>;
  by_trust_level: Record<string, number>;
  published_before_2025_count: number;
  earliest_published_at?: string | null;
  latest_published_at?: string | null;
  root: string;
}
export interface KnowledgeRetrievalResult { item: KnowledgeItem; score: number; matched_terms: string[] }
export interface KnowledgePreviewResponse {
  results: KnowledgeRetrievalResult[];
  formatted_prior: string;
  query: string;
  metadata: Record<string, unknown>;
  top_k: number;
  warning: string;
}
export interface KnowledgePairedEffect {
  sample_count?: number;
  pairs?: number;
  minimum_recommended_samples?: number;
  sample_adequate?: boolean;
  claim?: string;
  success_delta?: number;
  on_minus_off_success_delta?: number;
  definition?: string;
  warning?: string;
  [key: string]: unknown;
}
export interface KnowledgeExperimentSummary {
  id: string;
  path: string;
  created_at?: string | null;
  started_at?: string | null;
  status: string;
  case_count: number;
  planned_jobs: number;
  record_count: number;
  paired_effect: KnowledgePairedEffect;
  manifest?: Record<string, unknown> | null;
  plan?: Record<string, unknown> | null;
  summary?: Record<string, unknown> | null;
  read_errors?: Record<string, string>;
}
export interface KnowledgeExperimentArtifact {
  name: string;
  path: string;
  size_bytes: number;
  omitted: boolean;
  reason?: string;
  data?: Record<string, unknown> | null;
  error?: string;
}
export interface KnowledgeExperimentDetail extends KnowledgeExperimentSummary {
  artifacts: KnowledgeExperimentArtifact[];
  artifact_count: number;
  artifacts_truncated: boolean;
  prior_policy: string;
  minimum_paired_samples: number;
}
// What still points at a model config. Deleting a config detaches rather than
// deletes run history, so `blocking` is what can actually refuse the delete.
export interface ModelConfigUsage {
  id: string;
  name: string;
  provider_type: string;
  delete_safe: boolean;
  blocking: string[];
  references: Record<string, number>;
}
export interface ApiEnvelope<T> { data: T }

// System settings. `default_model_label` is the resolved model name (or a note
// explaining why it is unavailable) so the UI never has to look the id up.
export interface SystemSettings {
  runner_url: string;
  runner_allowed_cidrs: string;
  runner_token_configured: boolean;
  runner: { reachable: boolean; details: Record<string, unknown> | string };
  // False when the deployment uses container-native Workers and no longer fronts
  // a legacy Kali Runner. The settings view then hides the Runner rows entirely
  // instead of showing a permanently unreachable service.
  legacy_kali_runner: boolean;
  default_model_config_id?: string | null;
  default_model_label?: string | null;
}

// Arena integration. `token_configured` is the only thing a read ever says about
// the credential -- the value itself never leaves the backend.
export interface ArenaWorkerEngineInput {
  engine_type: string;
  model_config_id?: string | null;
}
export interface ArenaConnection {
  id: string;
  name: string;
  base_url: string;
  token_configured: boolean;
  enabled: boolean;
  requests_per_minute: number;
  max_concurrency: number;
  last_probe_at?: string | null;
  last_probe_ok?: boolean | null;
  profile_json: Record<string, unknown>;
  default_model_config_id?: string | null;
  default_engine_type?: string | null;
  default_solver_mode: string;
  default_reason_model_config_id?: string | null;
  default_worker_engines: ArenaWorkerEngineInput[];
  created_at: string;
  updated_at: string;
}
export interface ArenaConnectionInput {
  name: string;
  base_url: string;
  token?: string;
  enabled: boolean;
  requests_per_minute: number;
  max_concurrency: number;
  default_model_config_id?: string | null;
  default_engine_type?: string | null;
  default_solver_mode?: string;
  default_reason_model_config_id?: string | null;
  default_worker_engines?: ArenaWorkerEngineInput[];
}
export interface ArenaCheck { name: string; ok: boolean; message: string }
// One bucket of the scorebook. `accept_rate` is null rather than 0 when nothing
// was attempted, so an empty bucket is not read as a total failure.
export interface ArenaBucket {
  attempted: number;
  accepted: number;
  rejected: number;
  duplicate_suppressed: number;
  circuit_open: number;
  accept_rate: number | null;
  token_cost: number | null;
  label?: string;
}
export interface ArenaStats {
  total: ArenaBucket;
  distinct_challenges_touched: number;
  distinct_challenges_solved: number;
  by_category: Record<string, ArenaBucket>;
  by_model: Record<string, ArenaBucket>;
  by_engine: Record<string, ArenaBucket>;
}
export interface ArenaStatus {
  checks: ArenaCheck[];
  connections: ArenaConnection[];
  stats: ArenaStats;
  // Liveness of the batch loop. Optional because the status endpoint predates
  // it: rendering a missing scheduler block is harmless, but a required field
  // would make every cached response a type error.
  scheduler?: ArenaQueueScheduler;
  queue_counts?: Record<string, number>;
}
export interface ArenaProbeResult {
  connection: ArenaConnection;
  checks: ArenaCheck[];
}
// One catalogue row. Only the fields the list rendering depends on are typed;
// the platform adds more, and an untyped extra is harmless here.
export interface ArenaChallenge {
  id: string;
  friendly_id?: string;
  name?: string;
  category?: string;
  difficulty?: string;
  points?: number;
  has_attachments?: boolean;
  has_container?: boolean;
  is_solved?: boolean;
  solve_count?: number;
  practice_ground_id?: string;
  practice_ground_name?: string;
  [key: string]: unknown;
}
export interface ArenaChallengePage {
  items: ArenaChallenge[];
  total: number | null;
  limit_applied: number;
  categories: Record<string, number>;
  // False when the platform ignores `offset`. Stated in the payload so the UI can
  // say "first N of the catalogue" instead of implying the list is complete.
  paging_supported: boolean;
}
export interface ArenaSubmission {
  id: string;
  connection_id: string;
  platform_challenge_id: string;
  challenge_name: string;
  category: string;
  verdict: string;
  submitted_at: string;
  model_config_id?: string | null;
  engine_key?: string | null;
  token_cost?: number | null;
}

// MCP-backed reads. The ground list and challenge detail only exist on the
// /api/ai/v1/mcp surface; the REST catalogue cannot return them.
export interface ArenaGround {
  id: string;
  friendly_id?: string;
  name?: string;
  description?: string;
  challenge_count?: number;
  is_active?: boolean;
  [key: string]: unknown;
}
// The grounds endpoint returns the platform's envelope verbatim
// (``{items, total?}``), not a bare array. Typing it as an array was what made
// the page render fine until the data arrived and then white-screen on
// ``.map`` -- the annotation hid a real shape mismatch from the compiler.
export interface ArenaGroundPage {
  items: ArenaGround[];
  total?: number | null;
}
export interface ArenaMcpChallenge extends ArenaChallenge {
  is_visible?: boolean;
  requires_running_target_for_submit?: boolean;
  sub_flag_count?: number;
  max_attempts?: number;
}
export interface ArenaMcpChallengePage {
  items: ArenaMcpChallenge[];
  total: number | null;
  // Echoed from the platform so the UI can page without guessing where the
  // catalogue ends; null when the platform omits them.
  page?: number | null;
  total_page?: number | null;
  container_count: number;
  attachment_count: number;
}
export interface ArenaOrphans {
  active_environments: number | null;
  max_environments: number | null;
  orphan_suspected: boolean;
  note: string;
}
// The result of actively destroying a leaked environment. `orphans` is the
// platform's own count re-read afterwards, so success is confirmed by the slot
// being free rather than by the delete call not throwing.
export interface ArenaOrphanReclaim {
  reclaimed: boolean;
  reason: string;
  error?: string;
  lease?: Record<string, unknown> | null;
  orphans: ArenaOrphans;
}
export interface ArenaSolveOutcome {
  platform_challenge_id: string;
  local_challenge_id: string;
  run_id: string;
  run_status: string;
  environment: Record<string, unknown>;
  flag_candidates: string[];
  submissions: Array<Record<string, unknown>>;
  verdict: string | null;
  error: string | null;
  notes: string[];
}
// A solve takes minutes, so the POST returns a handle and the caller polls.
export interface ArenaSolveTask {
  task_id?: string;
  state: "running" | "finished" | "failed";
  connection_id?: string;
  outcome?: ArenaSolveOutcome;
  error?: string;
}

// Batch queue. A batch spans hours because the platform serves one environment
// at a time, so the queue is persisted and its progress is polled.
export interface ArenaQueueItem {
  id: string;
  connection_id: string;
  practice_ground_id: string;
  platform_challenge_id: string;
  challenge_name: string;
  category: string;
  difficulty: string;
  // True when this item needs the platform's single environment slot.
  has_container: boolean;
  status: "PENDING" | "RUNNING" | "SOLVED" | "UNSOLVED" | "FAILED" | "CANCELLED";
  priority: number;
  verdict?: string | null;
  // Times claimed, and times a solve reached a verdict. The retry budget reads
  // outcome_count, so an item showing attempt_count 2 / outcome_count 0 was
  // claimed twice by restarts and never actually ran to a result.
  attempt_count: number;
  outcome_count: number;
  last_error?: string | null;
  run_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
}
// Liveness of the batch scheduler. A batch runs for hours with nobody watching,
// so "is the loop still ticking" has to be readable; a stopped scheduler and a
// scheduler waiting on a slow challenge look identical from the queue alone.
export interface ArenaQueueScheduler {
  running: boolean;
  current_item_id: string | null;
  // Age of the item in flight, and age of the last loop iteration. They are
  // separate because a long tick age with an item in flight is a long item, not
  // a wedge -- the loop is inside the solve and not ticking.
  current_item_age_seconds: number | null;
  last_tick_age_seconds: number | null;
  uptime_seconds: number | null;
  processed_items: number;
  watchdog_timeouts: number;
  consecutive_failures: number;
  last_error: string | null;
  watchdog_seconds: number;
}
export interface ArenaQueuePage {
  items: ArenaQueueItem[];
  counts: Record<string, number>;
  scheduler: ArenaQueueScheduler;
}
export interface ArenaEnqueueRequest {
  practice_ground_id: string;
  category?: string;
  difficulty?: string;
  order: "offline_first" | "container_first" | "as_listed";
  skip_solved: boolean;
  limit: number;
}
export interface ArenaEnqueueResult {
  added: number;
  skipped: number;
  matched: number;
  selected: number;
  order: string;
  // Surfaced so "the ground has nothing left" is distinguishable from "the scan
  // stopped at the page cap" -- the two are otherwise identical to a caller.
  catalogue_total?: number;
  scan_truncated?: boolean;
}
export interface ArenaEnqueueByIdRequest {
  practice_ground_id: string;
  challenge_ids: string[];
  priority?: number;
}
export interface ArenaEnqueueByIdResult {
  added: number;
  skipped: number;
  // Named, not just counted: a caller that mistyped an id needs to know which.
  unreadable_ids: string[];
  not_in_catalogue: number;
}
// One bulk action over an explicit selection. Guarded per row server-side, so a
// single ineligible row (cancelling something already solved) cannot abort the
// rest, and the skips come back named.
export type ArenaQueueBulkAction = "retry" | "cancel" | "top" | "delete";
export interface ArenaQueueBulkResult {
  action: ArenaQueueBulkAction;
  changed: number;
  skipped: Array<{ id: string; reason: string }>;
  missing: string[];
}

