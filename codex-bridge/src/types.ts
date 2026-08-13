export interface ThreadRequest { run_id: string; workspace_path: string; prompt: string; scope?: Record<string, unknown> }
export interface ThreadResponse { thread_id: string; status: "created" }
export interface BridgeEvent { type: string; payload: Record<string, unknown>; status?: string }
export interface ThreadRunRequest { prompt: string; output_schema?: Record<string, unknown> }
