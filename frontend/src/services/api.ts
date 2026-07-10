import type { ApiEnvelope, Challenge, RunEvent, SolveRun } from "../types/api";

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
  listRuns: () => request<SolveRun[]>("/runs"),
  getRun: (id: string) => request<SolveRun>(`/runs/${id}`),
  createRun: (challengeId: string, engine_type = "mock") => request<SolveRun>(`/challenges/${challengeId}/runs`, { method: "POST", body: JSON.stringify({ engine_type }) }),
  startRun: (id: string) => request<{ run_id: string; status: string }>(`/runs/${id}/start`, { method: "POST" }),
  cancelRun: (id: string) => request<SolveRun>(`/runs/${id}/cancel`, { method: "POST" }),
  listModelConfigs: () => request<Array<{ id: string; name: string; provider_type: string; base_url?: string; model_name?: string; enabled: boolean }>>("/model-configs"),
  streamRunEvents: (id: string, onEvent: (event: RunEvent) => void) => { const source = new EventSource(`${base}/runs/${id}/events`); const handler = (message: MessageEvent<string>) => onEvent(JSON.parse(message.data) as RunEvent); source.onmessage = handler; runEventTypes.forEach((type) => source.addEventListener(type, handler)); return source; },
};
