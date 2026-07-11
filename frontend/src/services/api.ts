import type { ApiEnvelope, Challenge, ChallengeConversation, ChallengeMessage, RunEvent, Skill, SolveRun } from "../types/api";

const base = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";
const runEventTypes = ["run.created", "run.started", "run.status_changed", "agent.message", "agent.plan_created", "agent.hypothesis_created", "agent.hypothesis_updated", "tool.requested", "tool.started", "tool.output", "tool.completed", "tool.failed", "artifact.created", "flag.candidate_found", "flag.verified", "report.started", "report.completed", "run.completed", "run.failed"];
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, { headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }, ...init });
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.message ?? `HTTP ${response.status}`); }
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
  getModelSkills: (id: string) => request<Array<Record<string, unknown>>>(`/model-configs/${id}/skills`),
  setModelSkills: (id: string, payload: Array<Record<string, unknown>>) => request<Array<Record<string, unknown>>>(`/model-configs/${id}/skills`, { method: "PUT", body: JSON.stringify(payload) }),
  getChallengeSkills: (id: string) => request<Array<Record<string, unknown>>>(`/challenges/${id}/skills`),
  setChallengeSkills: (id: string, payload: Array<Record<string, unknown>>) => request<Array<Record<string, unknown>>>(`/challenges/${id}/skills`, { method: "PUT", body: JSON.stringify(payload) }),
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
  createRun: (challengeId: string, payload: Record<string, unknown>) => request<SolveRun>(`/challenges/${challengeId}/runs`, { method: "POST", body: JSON.stringify(payload) }),
  startRun: (id: string) => request<{ run_id: string; status: string }>(`/runs/${id}/start`, { method: "POST" }),
  cancelRun: (id: string) => request<SolveRun>(`/runs/${id}/cancel`, { method: "POST" }),
  listModelConfigs: () => request<Array<{ id: string; name: string; provider_type: string; base_url?: string; model_name?: string; enabled: boolean; api_key_configured: boolean }>>("/model-configs"),
  createModelConfig: (payload: Record<string, unknown>) => request<unknown>("/model-configs", { method: "POST", body: JSON.stringify(payload) }),
  updateModelConfig: (id: string, payload: Record<string, unknown>) => request<unknown>(`/model-configs/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteModelConfig: (id: string) => request<void>(`/model-configs/${id}`, { method: "DELETE" }),
  testModelConfig: (id: string) => request<{ ok: boolean; message: string }>(`/model-configs/${id}/test`, { method: "POST" }),
  getSystemSettings: () => request<{ runner_url: string; runner_allowed_cidrs: string; runner_token_configured: boolean; runner: { reachable: boolean; details: Record<string, unknown> | string }; codex_bridge_url: string; codex_bridge: { reachable: boolean; details: Record<string, unknown> | string } }>("/system-settings"),
  updateSystemSettings: (payload: { runner_url: string; codex_bridge_url: string }) => request<unknown>("/system-settings", { method: "PUT", body: JSON.stringify(payload) }),
  getToolCalls: (id: string) => request<Array<Record<string, unknown>>>(`/runs/${id}/tool-calls`),
  getObservations: (id: string) => request<Array<Record<string, unknown>>>(`/runs/${id}/observations`),
  getArtifacts: (id: string) => request<Array<{ id: string; path: string; type: string; summary: string; size: number }>>(`/runs/${id}/artifacts`),
  getArtifact: (runId: string, artifactId: string) => request<{ content: string; path: string }>(`/runs/${runId}/artifacts/${artifactId}`),
  getFlags: (id: string) => request<Array<Record<string, unknown>>>(`/runs/${id}/flag-candidates`),
  getReport: (id: string) => request<{ content: string; path: string }>(`/runs/${id}/report`),
  continueRun: (id: string, message: string) => request<{ run_id: string }>(`/runs/${id}/continue`, { method: "POST", body: JSON.stringify({ message }) }),
  streamRunEvents: (id: string, onEvent: (event: RunEvent) => void) => { const source = new EventSource(`${base}/runs/${id}/events`); const handler = (message: MessageEvent<string>) => onEvent(JSON.parse(message.data) as RunEvent); source.onmessage = handler; runEventTypes.forEach((type) => source.addEventListener(type, handler)); return source; },
};
