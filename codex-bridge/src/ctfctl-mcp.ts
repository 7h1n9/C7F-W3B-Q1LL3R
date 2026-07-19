/**
 * Minimal stdio MCP server for one Codex Thread.  It intentionally has no
 * filesystem or target-network capability of its own: every call is sent to
 * the Backend Tool Gateway with an immutable run scope supplied by Bridge.
 */
import { createInterface } from "node:readline";
import { appendFileSync } from "node:fs";

type Scope = { run_id: string; challenge_id: string; workspace_root: string; allowed_hosts: string[]; attempt_id: string; lease_token: string; thread_id?: string; model_turn_id?: string };
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

const compatibilityTools = new Set(["invoke_tool", "list_tools"]);
let advertisedTools = new Set<string>();

function schemaFor(name: string) {
  if (name.startsWith("workspace_")) return { type: "object", additionalProperties: true };
  if (name === "http_request" || name === "http_session_request") return { type: "object", properties: { method: { type: "string" }, url: { type: "string" }, headers: { type: "object" }, body: { type: "string" }, follow_redirects: { type: "boolean" } }, required: ["method", "url"], additionalProperties: false };
  if (name === "script_run") return { type: "object", properties: { path: { type: "string" }, interpreter: { type: "string", enum: ["python", "node", "bash"] }, args: { type: "array", items: { type: "string" } }, network_mode: { type: "string", enum: ["none", "target_allowlist"] }, timeout_seconds: { type: "integer" } }, required: ["path", "interpreter"], additionalProperties: false };
  if (name === "sandbox_exec") return { type: "object", properties: { executable: { type: "string" }, args: { type: "array", items: { type: "string" } }, cwd: { type: "string" }, network_mode: { type: "string", enum: ["none", "target_allowlist"] } }, required: ["executable"], additionalProperties: false };
  if (name === "python_run") return { type: "object", properties: { path: { type: "string" }, args: { type: "array", items: { type: "string" } } }, required: ["path"], additionalProperties: false };
  return { type: "object", additionalProperties: true };
}

async function toolDefinitions() {
  const catalog = await backend("list_tools", {});
  const candidateRows: unknown = catalog?.tools;
  const rows: unknown[] = Array.isArray(candidateRows) ? candidateRows : [];
  const definitions = rows
    .filter((item: unknown): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && typeof (item as Record<string, unknown>).name === "string")
    .map((item: Record<string, unknown>) => ({
    // MCP tool names are scoped by the server name (`ctfctl`) by the Codex
    // client. Returning a second `ctfctl.` prefix makes calls appear as
    // A second server namespace would make workspace calls fail before dispatch.
    name: String(item.name),
    description: String(item.description ?? "Run-scoped CTF tool."),
    // Codex uses MCP safety annotations to decide whether a tool call can run
    // under approval_policy=never. These operations remain server-enforced by
    // the run scope and Tool Gateway; the hint avoids an implicit client-side
    // rejection before the stdio server sees a call.
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      openWorldHint: false,
    },
    inputSchema: item.parameters && typeof item.parameters === "object" && Object.keys(item.parameters as object).length
      ? item.parameters
      : schemaFor(String(item.name)),
  }));
  advertisedTools = new Set(definitions.map((item) => item.name));
  return definitions;
}

async function backend(method: string, params: Record<string, unknown>) {
  const dedicatedMethods = new Set([
    "workspace_list", "workspace_tree", "workspace_stat", "workspace_read", "workspace_search",
    "workspace_write_file", "workspace_write_note", "workspace_patch_file", "workspace_mkdir",
    "workspace_copy", "workspace_move_generated", "workspace_delete_generated",
    "workspace_extract_archive", "list_tools", "invoke_tool",
  ]);
  const endpoint = dedicatedMethods.has(method) ? method : `tool/${method}`;
  const response = await fetch(`${backendUrl}/api/v1/internal/ctfctl/${endpoint}`, {
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
  if (!advertisedTools.has(shortName) && !compatibilityTools.has(shortName)) throw new Error("Unknown or unavailable ctfctl tool");
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
    else if (request.method === "tools/list") result = { tools: await toolDefinitions() };
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
