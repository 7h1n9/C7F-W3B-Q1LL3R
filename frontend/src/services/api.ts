import type { ApiEnvelope, ArenaChallengePage, ArenaConnection, ArenaConnectionInput, ArenaEnqueueByIdRequest, ArenaEnqueueByIdResult, ArenaEnqueueRequest, ArenaEnqueueResult, ArenaGroundPage, ArenaMcpChallengePage, ArenaOrphanReclaim, ArenaOrphans, ArenaProbeResult, ArenaQueueBulkAction, ArenaQueueBulkResult, ArenaQueueItem, ArenaQueuePage, ArenaQueueScheduler, ArenaSolveTask, ArenaStats, ArenaStatus, ArenaSubmission, Challenge, ChallengeConversation, ChallengeMessage, FlagCandidate, KnowledgeExperimentDetail, KnowledgeExperimentSummary, KnowledgeListResponse, KnowledgePreviewResponse, KnowledgeStats, ModelConfig, ModelConfigUsage, MutekiBoardSemantic, MutekiGraphState, RunControlExperimentRequest, RunControlExperimentResponse, RunDiagnostics, RunEvent, RunHealth, RunUsage, RunUserInput, Skill, SolveRun, SolverState, SystemSettings } from "../types/api";

const base = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";
const runEventTypes = [
  "run.created", "run.started", "run.restarted", "run.status_changed", "phase.corrected",
  "agent.message", "agent.plan_created", "agent.hypothesis_created", "agent.hypothesis_updated",
  "agent.action_requested", "agent.action_rejected", "agent.action_completed", "agent.replan_required",
  "agent.progress_detected", "agent.no_progress", "skill.requested", "skill.snapshot_created",
  "skill.activated", "skill.deactivated", "skill.declined", "skill.recommended", "skill.activation_rejected",
  "tool.requested", "tool.started", "tool.output", "tool.completed", "tool.failed", "artifact.created",
  "flag.candidate_found", "flag.reviewed", "flag.verified", "report.started", "report.completed",
  "run.completed", "run.failed", "user_input.received", "user_input.consumed", "user.input_consumed",
  "solver.run.started", "solver.action.planned", "solver.action.authorized", "solver.action.started",
  "solver.action.completed", "solver.action.failed", "solver.action.interrupted", "solver.action.recovered",
  "solver.tool.called", "solver.observation.received", "solver.completion.evaluated", "solver.run.completed",
  "solver.run.failed", "solver.step.completed",
  "muteki.run.started", "muteki.run.titled", "muteki.run_finished", "muteki.phase_changed", "muteki.prepare.engine.checked",
  "muteki.fact_added", "muteki.dead_end", "muteki.intent_proposed", "muteki.intent_claimed",
  "muteki.intent_released", "muteki.intent_concluded", "muteki.flag_candidate", "muteki.flag_found",
  "muteki.poc_saved", "muteki.resource_locked", "muteki.resource_released", "muteki.worker_started",
  "muteki.worker_step", "muteki.worker_command", "muteki.worker_finished", "muteki.review_finding", "muteki.review_proposal",
  "muteki.branch_split", "muteki.branch_resolved", "muteki.lane_locked", "muteki.lane_released",
  "muteki.intent_lane_deferred", "muteki.fact_challenged", "muteki.fact_revalidated",
  "muteki.fact_rejected", "muteki.fact_merged", "muteki.fact_superseded", "muteki.fact_pinned",
  "muteki.route_suppressed", "muteki.route_reopened", "muteki.review_proposal_decision",
  "muteki.coordinator_directive", "muteki.operator_directive", "muteki.operator_directive_status",
  "muteki.hitl_classified", "muteki.intent_state_changed", "muteki.graph_compacted",
  "cost.update", "muteki.cost.update",
];
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }, ...init });
  } catch {
    throw new Error("无法连接后端服务，请确认 127.0.0.1:8000 已启动");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.details?.errors?.[0]?.msg;
    const message = error.message === "Request validation failed." && detail ? detail : error.message;
    throw new Error(message || `后端请求失败（HTTP ${response.status}）`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json() as ApiEnvelope<T>).data;
}
export const api = {
  listChallenges: () => request<Challenge[]>("/challenges"),
  createChallenge: (payload: Omit<Challenge, "id" | "created_at" | "updated_at">) => request<Challenge>("/challenges", { method: "POST", body: JSON.stringify(payload) }),
  getChallenge: (id: string) => request<Challenge>(`/challenges/${id}`),
  updateChallenge: (id: string, payload: Omit<Challenge, "id" | "created_at" | "updated_at">) => request<Challenge>(`/challenges/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteChallenge: (id: string) => request<void>(`/challenges/${id}`, { method: "DELETE" }),
  listSkills: () => request<Skill[]>("/skills"),
  createSkill: (payload: Record<string, unknown>) => request<Skill>("/skills", { method: "POST", body: JSON.stringify(payload) }),
  updateSkill: (id: string, payload: Record<string, unknown>) => request<Skill>(`/skills/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  validateSkill: (id: string) => request<{ ok: boolean; message: string }>(`/skills/${id}/validate`, { method: "POST" }),
  duplicateSkill: (id: string) => request<Skill>(`/skills/${id}/duplicate`, { method: "POST" }),
  deleteSkill: (id: string) => request<void>(`/skills/${id}`, { method: "DELETE" }),
  listLearnedSkillCandidates: () => request<Array<Record<string, unknown>>>("/learned-skill-candidates"),
  reviewLearnedSkillCandidate: (id: string, decision: "APPROVE" | "REJECT") => request<Record<string, unknown>>(`/learned-skill-candidates/${id}/review`, { method: "POST", body: JSON.stringify({ decision, reviewer: "human" }) }),
  getModelSkills: (id: string) => request<Array<Record<string, unknown>>>(`/model-configs/${id}/skills`),
  setModelSkills: (id: string, payload: Array<Record<string, unknown>>) => request<Array<Record<string, unknown>>>(`/model-configs/${id}/skills`, { method: "PUT", body: JSON.stringify(payload) }),
  getChallengeSkills: (id: string) => request<Array<Record<string, unknown>>>(`/challenges/${id}/skills`),
  setChallengeSkills: (id: string, payload: Array<Record<string, unknown>>) => request<Array<Record<string, unknown>>>(`/challenges/${id}/skills`, { method: "PUT", body: JSON.stringify(payload) }),
  listKnowledgeItems: (params: { query?: string; kind?: string; trust_level?: string; target_stack?: string; limit?: number; offset?: number } = {}) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== "") search.set(key, String(value));
    });
    const query = search.toString();
    return request<KnowledgeListResponse>(`/knowledge/items${query ? `?${query}` : ""}`);
  },
  getKnowledgeStats: () => request<KnowledgeStats>("/knowledge/stats"),
  previewKnowledge: (payload: { query: string; metadata: Record<string, unknown>; top_k: number }) => request<KnowledgePreviewResponse>("/knowledge/preview", { method: "POST", body: JSON.stringify(payload) }),
  listKnowledgeExperiments: async () => {
    const response = await request<{ experiments: KnowledgeExperimentSummary[]; root: string }>("/knowledge/experiments");
    return response.experiments;
  },
  getKnowledgeExperiment: (id: string) => request<KnowledgeExperimentDetail>(`/knowledge/experiments/${encodeURIComponent(id)}`),
  listAttachments: (id: string) => request<Array<Record<string, unknown>>>(`/challenges/${id}/attachments`),
  uploadAttachment: async (id: string, file: File, isPrimary = false) => { const response = await fetch(`${base}/challenges/${id}/attachments?is_primary=${isPrimary}`, { method: "POST", body: (() => { const form = new FormData(); form.append("file", file); return form; })() }); if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.message ?? `HTTP ${response.status}`); } return (await response.json() as ApiEnvelope<Record<string, unknown>>).data; },
  listConversations: (id: string) => request<ChallengeConversation[]>(`/challenges/${id}/conversations`),
  createConversation: (id: string, payload: Record<string, unknown>) => request<ChallengeConversation>(`/challenges/${id}/conversations`, { method: "POST", body: JSON.stringify(payload) }),
  getConversation: (id: string) => request<ChallengeConversation & { skills: Array<{ skill_id: string; priority: number }> }>(`/conversations/${id}`),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, { method: "DELETE" }),
  listConversationMessages: (id: string) => request<ChallengeMessage[]>(`/conversations/${id}/messages`),
  sendConversationMessage: (id: string, content: string) => request<ChallengeMessage>(`/conversations/${id}/messages`, { method: "POST", body: JSON.stringify({ content }) }),
  createRunFromConversation: (id: string, payload: Record<string, unknown>) => request<SolveRun>(`/conversations/${id}/create-run`, { method: "POST", body: JSON.stringify(payload) }),
  getReadiness: () => request<{ ready: boolean; level: string; checks: Array<{ name: string; ok: boolean; message: string }> }>("/readiness/range-test"),
  listRuns: () => request<SolveRun[]>("/runs"),
  getRun: (id: string) => request<SolveRun>(`/runs/${id}`),
  createControlRun: (id: string, payload: RunControlExperimentRequest) => request<RunControlExperimentResponse>(`/runs/${id}/control`, { method: "POST", body: JSON.stringify(payload) }),
  getRunMessages: (id: string) => request<RunUserInput[]>(`/runs/${id}/messages`),
  getSolverState: (id: string) => request<SolverState>(`/runs/${id}/solver-state`),
  getMutekiState: (id: string) => request<MutekiGraphState>(`/runs/${id}/muteki-state`),
  startMutekiBoardSemanticAnalysis: (id: string) => request<MutekiBoardSemantic>(`/runs/${id}/muteki-board/semantic-analysis`, { method: "POST" }),
  downloadMutekiPoc: async (id: string) => {
    const response = await fetch(`${base}/runs/${id}/muteki-poc`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.message || `PoC 导出失败（HTTP ${response.status}）`);
    }
    return { filename: response.headers.get("content-disposition")?.match(/filename="([^"]+)"/)?.[1] ?? `muteki-${id}-answer-poc.md`, content: await response.text() };
  },
  downloadMutekiPocBundle: async (id: string) => {
    const response = await fetch(`${base}/runs/${id}/muteki-poc/download`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.message || `PoC 包导出失败（HTTP ${response.status}）`);
    }
    return {
      filename: response.headers.get("content-disposition")?.match(/filename="([^"]+)"/)?.[1] ?? `muteki-${id}-poc.zip`,
      blob: await response.blob(),
    };
  },
  downloadRunReport: async (id: string) => {
    const response = await fetch(`${base}/runs/${id}/report/download`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.message || `Writeup 导出失败（HTTP ${response.status}）`);
    }
    return {
      filename: response.headers.get("content-disposition")?.match(/filename="([^"]+)"/)?.[1] ?? `${id}-writeup.md`,
      blob: await response.blob(),
    };
  },
  getRunDiagnostics: (id: string) => request<RunDiagnostics>(`/runs/${id}/diagnostics`),
  getRunHealth: (id: string) => request<RunHealth>(`/runs/${id}/health`),
  getRunUsage: (id: string) => request<RunUsage>(`/runs/${id}/usage`),

  listRunDiagnostics: (limit = 25) => request<Array<{ run_id: string } & RunDiagnostics>>(`/diagnostics/runs?limit=${limit}`),
  createRun: (challengeId: string, payload: Record<string, unknown>) => request<SolveRun>(`/challenges/${challengeId}/runs`, { method: "POST", body: JSON.stringify(payload) }),
  startRun: (id: string) => request<{ run_id: string; status: string }>(`/runs/${id}/start`, { method: "POST" }),
  restartRun: (id: string, message = "") => request<{ run_id: string; status: string }>(`/runs/${id}/restart`, { method: "POST", body: JSON.stringify({ message }) }),
  cancelRun: (id: string) => request<SolveRun>(`/runs/${id}/cancel`, { method: "POST" }),
  deleteRun: (id: string) => request<void>(`/runs/${id}`, { method: "DELETE" }),
  deleteRuns: (runIds: string[]) => request<{ deleted_count: number; run_ids: string[] }>("/runs/batch", { method: "DELETE", body: JSON.stringify({ run_ids: runIds }) }),
  listModelConfigs: () => request<ModelConfig[]>("/model-configs"),
  createModelConfig: (payload: Record<string, unknown>) => request<unknown>("/model-configs", { method: "POST", body: JSON.stringify(payload) }),
  updateModelConfig: (id: string, payload: Record<string, unknown>) => request<unknown>(`/model-configs/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteModelConfig: (id: string) => request<void>(`/model-configs/${id}`, { method: "DELETE" }),
  getModelConfigUsage: (id: string) => request<ModelConfigUsage>(`/model-configs/${id}/usage`),
  testModelConfig: (id: string) => request<{ ok: boolean; message: string }>(`/model-configs/${id}/test`, { method: "POST" }),
  getSystemSettings: () => request<SystemSettings>("/system-settings"),
  updateSystemSettings: (payload: { runner_url?: string | null; default_model_config_id?: string | null }) => request<SystemSettings>("/system-settings", { method: "PUT", body: JSON.stringify(payload) }),
  getToolCalls: (id: string) => request<Array<Record<string, unknown>>>(`/runs/${id}/tool-calls`),
  getObservations: (id: string) => request<Array<Record<string, unknown>>>(`/runs/${id}/observations`),
  getArtifacts: (id: string) => request<Array<{ id: string; path: string; type: string; summary: string; size: number }>>(`/runs/${id}/artifacts`),
  getArtifact: (runId: string, artifactId: string) => request<{ content: string; path: string }>(`/runs/${runId}/artifacts/${artifactId}`),
  getFlags: (id: string) => request<FlagCandidate[]>(`/runs/${id}/flag-candidates`),
  reviewFlagCandidate: (runId: string, candidateId: string, reviewState: "OPEN" | "VALID" | "INVALID") => request<FlagCandidate>(`/runs/${runId}/flag-candidates/${candidateId}`, { method: "PATCH", body: JSON.stringify({ review_state: reviewState }) }),
  getReport: (id: string) => request<{ content: string; path: string; report_json?: Record<string, unknown>; flag_pattern?: string }>(`/runs/${id}/report`),
  continueRun: (id: string, message: string) => request<{ run_id: string }>(`/runs/${id}/continue`, { method: "POST", body: JSON.stringify({ message }) }),
  sendRunMessage: (id: string, content: string) => request<{ accepted: boolean; revision: number; status: string; message: string }>(`/runs/${id}/messages`, { method: "POST", body: JSON.stringify({ content }) }),
  streamRunEvents: (id: string, onEvent: (event: RunEvent) => void) => {
    const source = new EventSource(`${base}/runs/${id}/events`);
    const handler = (message: MessageEvent<string>) => {
      if (!message.data) return;
      try {
        onEvent(JSON.parse(message.data) as RunEvent);
      } catch {
        // Ignore malformed/heartbeat frames; the run-health poll remains the
        // authoritative fallback for a temporarily degraded event stream.
      }
    };
    source.onmessage = handler;
    runEventTypes.forEach((type) => source.addEventListener(type, handler));
    return source;
  },
  listArenaConnections: () => request<ArenaConnection[]>("/arena/connections"),
  createArenaConnection: (payload: ArenaConnectionInput) => request<ArenaConnection>("/arena/connections", { method: "POST", body: JSON.stringify(payload) }),
  updateArenaConnection: (id: string, payload: ArenaConnectionInput) => request<ArenaConnection>(`/arena/connections/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteArenaConnection: (id: string) => request<void>(`/arena/connections/${id}`, { method: "DELETE" }),
  probeArenaConnection: (id: string) => request<ArenaProbeResult>(`/arena/connections/${id}/probe`, { method: "POST" }),
  getArenaStatus: () => request<ArenaStatus>("/arena/status"),
  getArenaStats: (connectionId?: string) => request<ArenaStats>(`/arena/stats${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  listArenaSubmissions: (connectionId?: string) => request<ArenaSubmission[]>(`/arena/submissions${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  // The arena exposes exactly one catalogue route; categories are discovered from
  // the payload rather than enumerated, because the label is free-form.
  listArenaChallenges: (connectionId: string, category?: string, limit?: number) => {
    const params = new URLSearchParams({ connection_id: connectionId });
    if (category) params.set("category", category);
    if (limit) params.set("limit", String(limit));
    return request<ArenaChallengePage>(`/arena/challenges?${params.toString()}`);
  },
  // Everything below goes through the MCP surface, which is the only place the
  // ground list, challenge detail, environment lifecycle and submission exist.
  // Returns the platform envelope as-is; callers read ``.items``. The backend
  // forwards ``list_practice_grounds`` verbatim, so unwrapping here would hide
  // the real shape rather than simplify it.
  listArenaGrounds: (connectionId: string) => request<ArenaGroundPage>(`/arena/grounds?connection_id=${encodeURIComponent(connectionId)}`),
  listArenaMcpChallenges: (connectionId: string, practiceGroundId: string, options: { category?: string; difficulty?: string; unsolvedOnly?: boolean; page?: number; limit?: number } = {}) => {
    const params = new URLSearchParams({ connection_id: connectionId, practice_ground_id: practiceGroundId });
    if (options.category) params.set("category", options.category);
    if (options.difficulty) params.set("difficulty", options.difficulty);
    if (options.unsolvedOnly) params.set("unsolved_only", "true");
    if (options.page) params.set("page", String(options.page));
    params.set("limit", String(options.limit ?? 100));
    return request<ArenaMcpChallengePage>(`/arena/mcp-challenges?${params.toString()}`);
  },
  getArenaMcpChallenge: (connectionId: string, practiceGroundId: string, challengeId: string) => {
    const params = new URLSearchParams({ connection_id: connectionId, practice_ground_id: practiceGroundId });
    return request<Record<string, unknown>>(`/arena/mcp-challenges/${encodeURIComponent(challengeId)}?${params.toString()}`);
  },
  getArenaOrphans: (connectionId: string) => request<ArenaOrphans>(`/arena/orphans?connection_id=${encodeURIComponent(connectionId)}`),
  // Actively destroy a leaked environment. The lease breadcrumb supplies the
  // challenge name; the explicit parameters are the override for a lost record.
  reclaimArenaOrphans: (connectionId: string, options: { practiceGroundId?: string; challengeId?: string } = {}) => {
    const params = new URLSearchParams({ connection_id: connectionId });
    if (options.practiceGroundId) params.set("practice_ground_id", options.practiceGroundId);
    if (options.challengeId) params.set("challenge_id", options.challengeId);
    return request<ArenaOrphanReclaim>(`/arena/orphans/reclaim?${params.toString()}`, { method: "POST" });
  },
  startArenaSolve: (practiceGroundId: string, challengeId: string) => request<ArenaSolveTask>("/arena/solve", { method: "POST", body: JSON.stringify({ practice_ground_id: practiceGroundId, challenge_id: challengeId }) }),
  getArenaSolveTask: (taskId: string) => request<ArenaSolveTask>(`/arena/solve/${taskId}`),
  // Batch queue. Enqueue composes the batch server-side from the catalogue so the
  // client never has to page through hundreds of rate-limited reads.
  getArenaQueue: () => request<ArenaQueuePage>("/arena/queue"),
  enqueueArenaQueue: (payload: ArenaEnqueueRequest) => request<ArenaEnqueueResult>("/arena/queue/enqueue", { method: "POST", body: JSON.stringify(payload) }),
  // By-id enqueue reads each challenge from the platform, so a named challenge
  // past the first catalogue page is reachable.
  enqueueArenaQueueByIds: (payload: ArenaEnqueueByIdRequest) => request<ArenaEnqueueByIdResult>("/arena/queue/enqueue-ids", { method: "POST", body: JSON.stringify(payload) }),
  startArenaQueue: () => request<{ state: string }>("/arena/queue/start", { method: "POST" }),
  stopArenaQueue: () => request<{ state: string }>("/arena/queue/stop", { method: "POST" }),
  // Returns items stranded in RUNNING by a dead process to the backlog. Cheap and
  // idempotent; the scheduler's own in-flight item is left alone.
  recoverArenaQueue: () => request<{ repaired: number; scheduler: ArenaQueueScheduler }>("/arena/queue/recover", { method: "POST" }),
  cancelArenaQueueItem: (itemId: string) => request<ArenaQueueItem>(`/arena/queue/${itemId}/cancel`, { method: "POST" }),
  prioritizeArenaQueueItem: (itemId: string, position: "top" | "bottom") => request<ArenaQueueItem>(`/arena/queue/${itemId}/priority`, { method: "POST", body: JSON.stringify({ position }) }),
  bulkArenaQueue: (payload: { item_ids: string[]; action: ArenaQueueBulkAction }) => request<ArenaQueueBulkResult>("/arena/queue/bulk", { method: "POST", body: JSON.stringify(payload) }),
  clearArenaQueue: (status = "PENDING") => request<{ deleted: number; status: string }>(`/arena/queue/clear?status=${encodeURIComponent(status)}`, { method: "POST" }),
};
