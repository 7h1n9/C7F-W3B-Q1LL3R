/**
 * Minimal stdio MCP server for one Codex Thread.  It intentionally has no
 * filesystem or target-network capability of its own: every call is sent to
 * the Backend Tool Gateway with an immutable run scope supplied by Bridge.
 */
import { createInterface } from "node:readline";
import { appendFileSync } from "node:fs";

type Scope = { run_id: string; challenge_id: string; workspace_root: string; allowed_hosts: string[] };
const scope = JSON.parse(process.env.CTFCTL_SCOPE ?? "{}") as Scope;
const backendUrl = (process.env.CTFCTL_BACKEND_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const accessKey = process.env.CTFCTL_ACCESS_KEY ?? "";
const debugLog = process.env.CTFCTL_DEBUG_LOG;

function debug(event: string, detail: Record<string, unknown> = {}) {
  if (!debugLog) return;
  try {
    appendFileSync(debugLog, `${JSON.stringify({ at: new Date().toISOString(), event, ...detail })}\n`, "utf8");
  } catch {
    // MCP stdout is protocol-only. Diagnostics must never interfere with it.
  }
}

const tools = [
  ["workspace_list", "List files in the current run workspace only."],
  ["workspace_read", "Read a bounded line range from a current-workspace relative path."],
  ["workspace_search", "Search text in the current run workspace only."],
  ["workspace_write_note", "Write a note/script/final file under the current run workspace."],
  ["list_tools", "List backend-approved tools and JSON argument schemas for this run."],
  ["invoke_tool", "Invoke a backend-approved Runner tool through the Tool Gateway."],
] as const;

function toolDefinitions() {
  return tools.map(([name, description]) => ({
    // MCP tool names are scoped by the server name (`ctfctl`) by the Codex
    // client. Returning a second `ctfctl.` prefix makes calls appear as
    // `ctfctl.ctfctl.workspace_list` and can be cancelled before dispatch.
    name,
    description,
    // Codex uses MCP safety annotations to decide whether a tool call can run
    // under approval_policy=never. These operations remain server-enforced by
    // the run scope and Tool Gateway; the hint avoids an implicit client-side
    // rejection before the stdio server sees a call.
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      openWorldHint: false,
    },
    inputSchema: name === "workspace_read" ? { type: "object", properties: { path: { type: "string" }, start_line: { type: "integer" }, end_line: { type: "integer" }, max_chars: { type: "integer" } }, required: ["path"], additionalProperties: false }
      : name === "workspace_search" ? { type: "object", properties: { query: { type: "string" }, max_results: { type: "integer" } }, required: ["query"], additionalProperties: false }
      : name === "workspace_write_note" ? { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"], additionalProperties: false }
      : name === "invoke_tool" ? { type: "object", properties: { tool: { type: "string" }, arguments: { type: "object" } }, required: ["tool", "arguments"], additionalProperties: false }
      : { type: "object", properties: {}, additionalProperties: false },
  }));
}

async function backend(method: string, params: Record<string, unknown>) {
  const response = await fetch(`${backendUrl}/api/v1/internal/ctfctl/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-ctfctl-access-key": accessKey },
    body: JSON.stringify({ scope, ...params }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`${body.code ?? "CTFCTL_ERROR"}: ${body.message ?? response.statusText}`);
  return body.data ?? body;
}

async function dispatch(name: string, args: Record<string, unknown>) {
  const shortName = name.replace(/^ctfctl\./, "");
  if (!tools.some(([tool]) => tool === shortName)) throw new Error("Unknown ctfctl tool");
  return backend(shortName, args);
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  let requestId: string | number | undefined;
  try {
    const request = JSON.parse(line) as { id?: string | number; method?: string; params?: Record<string, unknown> };
    requestId = request.id;
    debug("request", { id: request.id ?? null, method: request.method ?? null });
    if (request.method === "notifications/initialized") continue;
    let result: unknown;
    if (request.method === "initialize") result = { protocolVersion: "2024-11-05", capabilities: { tools: {} }, serverInfo: { name: "ctfctl", version: "1.0.0" } };
    else if (request.method === "tools/list") result = { tools: toolDefinitions() };
    else if (request.method === "tools/call") {
      const params = request.params ?? {};
      const value = await dispatch(String(params.name ?? ""), (params.arguments ?? {}) as Record<string, unknown>);
      result = { content: [{ type: "text", text: JSON.stringify(value) }] };
    } else throw new Error(`Unsupported MCP method: ${request.method}`);
    debug("response", { id: request.id ?? null, method: request.method ?? null, ok: true });
    if (request.id !== undefined) process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    debug("response", { id: requestId ?? null, ok: false, error: message });
    if (requestId !== undefined) process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: requestId, error: { code: -32000, message } })}\n`);
  }
}
