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

const tools = [
  ["workspace_list", "List files in the current run workspace only."],
  ["workspace_tree", "Show the allowed workspace tree and manifest."],
  ["workspace_stat", "Stat one readable workspace path."],
  ["workspace_read", "Read a bounded line range from a current-workspace relative path."],
  ["workspace_search", "Search text in the current run workspace only."],
  ["workspace_write_file", "Create a generated workspace file and sync it to Runner."],
  ["workspace_patch_file", "Apply one exact text or line-range patch to a generated file."],
  ["workspace_mkdir", "Create a generated workspace directory."],
  ["workspace_copy", "Copy readable workspace material into a generated area."],
  ["workspace_move_generated", "Move generated material into a final draft."],
  ["workspace_delete_generated", "Delete only agent-generated material."],
  ["workspace_extract_archive", "Safely extract an attachment/archive into extracted/."],
  ["workspace_write_note", "Write a note/script/final file under the current run workspace."],
  ["http_request", "Send one allowlisted HTTP request."],
  ["http_session_request", "Send one request using the bounded HTTP session."],
  ["http_extract", "Extract structured facts from an HTTP response artifact."],
  ["content_discovery", "Run bounded content discovery against the target."],
  ["python_run", "Run a legacy Python script through the Runner."],
  ["script_run", "Run a synced Python, Node, or Bash script."],
  ["sandbox_exec", "Run one allowlisted offline executable with argv only."],
  ["jwt_inspect", "Inspect a JWT without network access."],
  ["file_type", "Identify a workspace file type."],
  ["strings_extract", "Extract printable strings from a workspace file."],
] as const;

const compatibilityTools = new Set(["invoke_tool", "list_tools"]);

function schemaFor(name: string) {
  if (name.startsWith("workspace_")) return { type: "object", additionalProperties: true };
  if (name === "http_request" || name === "http_session_request") return { type: "object", properties: { method: { type: "string" }, url: { type: "string" }, headers: { type: "object" }, body: { type: "string" }, follow_redirects: { type: "boolean" } }, required: ["method", "url"], additionalProperties: false };
  if (name === "script_run") return { type: "object", properties: { path: { type: "string" }, interpreter: { type: "string", enum: ["python", "node", "bash"] }, args: { type: "array", items: { type: "string" } }, network_mode: { type: "string", enum: ["none", "target_allowlist"] }, timeout_seconds: { type: "integer" } }, required: ["path", "interpreter"], additionalProperties: false };
  if (name === "sandbox_exec") return { type: "object", properties: { executable: { type: "string" }, args: { type: "array", items: { type: "string" } }, cwd: { type: "string" }, network_mode: { type: "string", enum: ["none", "target_allowlist"] } }, required: ["executable"], additionalProperties: false };
  if (name === "python_run") return { type: "object", properties: { path: { type: "string" }, args: { type: "array", items: { type: "string" } } }, required: ["path"], additionalProperties: false };
  return { type: "object", additionalProperties: true };
}

function toolDefinitions() {
  return tools.map(([name, description]) => ({
    // MCP tool names are scoped by the server name (`ctfctl`) by the Codex
    // client. Returning a second `ctfctl.` prefix makes calls appear as
    // A second server namespace would make workspace calls fail before dispatch.
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
      : schemaFor(name),
  }));
}

async function backend(method: string, params: Record<string, unknown>) {
  const directTools = new Set(["http_request", "http_session_request", "http_extract", "content_discovery", "python_run", "script_run", "jwt_inspect", "file_type", "strings_extract", "sandbox_exec"]);
  const endpoint = directTools.has(method) ? `tool/${method}` : method;
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
  if (!tools.some(([tool]) => tool === shortName) && !compatibilityTools.has(shortName)) throw new Error("Unknown ctfctl tool");
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
