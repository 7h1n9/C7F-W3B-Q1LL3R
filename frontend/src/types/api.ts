export interface Challenge { id: string; name: string; description: string; target_url: string; allowed_hosts: string[]; flag_pattern: string; source_path?: string | null; status: string; created_at: string; updated_at: string }
export interface SolveRun { id: string; challenge_id: string; engine_type: string; status: string; current_phase: string; workspace_path: string; started_at?: string | null; finished_at?: string | null; created_at: string; updated_at: string }
export interface RunEvent { id: string; run_id: string; sequence: number; event_type: string; payload_json: Record<string, unknown>; created_at: string }
export interface ApiEnvelope<T> { data: T }
