export interface ThreadRequest { run_id: string; workspace_path: string; prompt: string }
export interface ThreadResponse { thread_id: string; status: "created" }
export interface BridgeEvent { type: string; payload: Record<string, unknown>; status?: string }
