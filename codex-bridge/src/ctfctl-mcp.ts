/**
 * Minimal stdio MCP server for one Codex Thread.  It intentionally has no
 * filesystem or target-network capability of its own: every call is sent to
 * the Backend Tool Gateway with an immutable run scope supplied by Bridge.
 */
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { parametersToInputSchema, validateMcpInputSchema } from "./mcp-schema.js";

type Scope = { run_id: string; challenge_id: string; workspace_root: string; allowed_hosts: string[]; attempt_id: string; lease_token: string; master_lease_token?: string; thread_id?: string; model_turn_id?: string; turn_id?: string; agent_task_id?: string; agent_role?: string; task_lease_token?: string; allowed_tools?: string[] };
let scope = JSON.parse(process.env.CTFCTL_SCOPE ?? "{}") as Scope;
const masterScope = { ...scope, master_lease_token: scope.master_lease_token ?? scope.lease_token };
const backendUrl = (process.env.CTFCTL_BACKEND_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const accessKey = process.env.CTFCTL_ACCESS_KEY ?? "";
const debugLog = process.env.CTFCTL_DEBUG_LOG;

function debug(event: string, detail: Record<string, unknown> = {}) {
  if (!debugLog) return;
  try {
    mkdirSync(dirname(debugLog), { recursive: true });
    appendFileSync(debugLog, `${JSON.stringify({ at: new Date().toISOString(), event, ...detail })}\n`, "utf8");
  } catch {
    // MCP stdout is protocol-only. Diagnostics must never interfere with it.
  }
}

const compatibilityTools = new Set(["invoke_tool", "list_tools"]);
let advertisedTools = new Set<string>();
const rejectedCalls = new Map<string, number>();
let turnStopReason: string | undefined;

type ErrorEnvelope = { code: string; message: string; stage: string; retryable: boolean; diagnostic_id: string; tool_execution_completed: boolean; details?: unknown };
class McpEnvelopeError extends Error {
  envelope: ErrorEnvelope;
  constructor(envelope: ErrorEnvelope) {
    super(envelope.message);
    this.envelope = envelope;
  }
}

function responseEnvelope(body: unknown, fallbackCode: string, fallbackMessage: string): ErrorEnvelope {
  const value = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  return {
    code: String(value.code ?? fallbackCode),
    message: String(value.message ?? fallbackMessage),
    stage: String(value.stage ?? "MCP"),
    retryable: Boolean(value.retryable ?? false),
    diagnostic_id: String(value.diagnostic_id ?? randomUUID()),
    tool_execution_completed: Boolean(value.tool_execution_completed ?? false),
    details: value.details,
  };
}

async function toolDefinitions() {
  const catalog = await backend("list_tools", {});
  const candidateRows: unknown = catalog?.tools;
  const rows: unknown[] = Array.isArray(candidateRows) ? candidateRows : [];
  const rejected: Array<{ name: string; errors: string[]; fallback_used: boolean }> = [];
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
    inputSchema: parametersToInputSchema(item.parameters, String(item.name)),
  }))
    .filter((item) => {
      const errors = validateMcpInputSchema(item.inputSchema);
      if (errors.length) {
        rejected.push({ name: item.name, errors, fallback_used: true });
        // Never turn a malformed critical schema into an unconstrained
        // additionalProperties=true tool.  A drifted catalog is blocked by
        // preflight and the Attempt is paused for deployment repair.
        if (["sql_boolean_compare", "boolean_config_extract", "script_run", "sandbox_exec", "workspace_write_file"].includes(item.name)) {
          throw new Error(`MCP_SCHEMA_DEGRADED:${item.name}`);
        }
      }
      return errors.length === 0;
    });
  if (rejected.length) debug("mcp_tool_schema_rejected", { rejected });
  if (definitions.length === 0) throw new Error("CTFCTL_TOOL_CATALOG_EMPTY");
  advertisedTools = new Set(definitions.map((item) => item.name));
  return definitions;
}

async function backend(method: string, params: Record<string, unknown>, logicalToolCallId?: string) {
  if (turnStopReason) {
    return {
      status: "STOPPED",
      stop: true,
      reason: turnStopReason,
      instruction: "Stop invoking ctfctl tools for this turn and summarize the durable evidence.",
    };
  }
  const dedicatedMethods = new Set([
    "workspace_list", "workspace_tree", "workspace_stat", "workspace_read", "workspace_search",
    "workspace_write_file", "workspace_write_note", "workspace_patch_file", "workspace_mkdir",
    "workspace_copy", "workspace_move_generated", "workspace_delete_generated",
    "workspace_extract_archive", "list_tools", "invoke_tool",
  ]);
  const endpoint = dedicatedMethods.has(method) ? method : `tool/${method}`;
  // Each call gets an independent one-shot ticket. Never reuse that ticket;
  // the immutable master lease remains in the thread scope for ticket minting.
  if (method !== "tool-ticket") {
    const ticketResponse = await fetch(`${backendUrl}/api/v1/internal/ctfctl/tool-ticket`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-ctfctl-access-key": accessKey },
      body: JSON.stringify({
        run_id: masterScope.run_id,
        current_attempt_id: masterScope.attempt_id,
        thread_id: masterScope.thread_id ?? null,
        model_turn_id: masterScope.model_turn_id ?? logicalToolCallId ?? null,
      }),
    });
    const ticketBody = await ticketResponse.json().catch(() => ({}));
    if (!ticketResponse.ok) throw new McpEnvelopeError(responseEnvelope(ticketBody, "MCP_VALIDATION_FAILED", ticketResponse.statusText));
    const ticket = ticketBody.data?.ticket;
    if (typeof ticket !== "string" || !ticket) throw new Error("CTFCTL_TICKET_INVALID");
    // Keep the master lease immutable. The one-shot ticket is sent as a
    // separate request field and never replaces the scope credential.
    const requestScope = {
      ...masterScope,
      lease_token: masterScope.master_lease_token,
      logical_tool_call_id: logicalToolCallId,
      turn_id: masterScope.model_turn_id ?? logicalToolCallId,
    };
    const response = await fetch(`${backendUrl}/api/v1/internal/ctfctl/${endpoint}`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-ctfctl-access-key": accessKey },
      body: JSON.stringify({ scope: requestScope, tool_ticket: ticket, ...params }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const envelope = responseEnvelope(body, "MCP_VALIDATION_FAILED", response.statusText);
      const signature = `${method}:${JSON.stringify(params, Object.keys(params).sort())}:${envelope.code}`;
      const count = (rejectedCalls.get(signature) ?? 0) + 1;
      rejectedCalls.set(signature, count);
      debug("backend_error", {
        method,
        status: response.status,
        code: envelope.code,
        rejection_count: count,
        argument_keys: Object.keys(params).sort(),
        details: envelope.details,
      });
      if (
        response.status === 429
        && [
          "TURN_TOOL_BUDGET_EXHAUSTED",
          "ATTEMPT_TOOL_BUDGET_EXHAUSTED",
          "RUN_MAX_TOOL_CALLS",
          "REQUIRED_ACTION_BUDGET_EXHAUSTED",
        ].includes(envelope.code)
      ) {
        turnStopReason = envelope.code;
        return {
          status: "STOPPED",
          stop: true,
          reason: turnStopReason,
          instruction: "The bounded tool budget is exhausted. Stop invoking tools for this turn and summarize the durable evidence.",
        };
      }
      if (count >= 3) {
        envelope.code = "MCP_REPEATED_REJECTION";
        envelope.retryable = false;
        envelope.message = `${envelope.message} Do not retry this exact ${method} call; choose a different bounded action.`;
        turnStopReason = envelope.code;
        return {
          status: "STOPPED",
          stop: true,
          reason: turnStopReason,
          instruction: "The same tool call was rejected repeatedly. Stop this turn and choose a different bounded action next time.",
        };
      }
      throw new McpEnvelopeError(envelope);
    }
    return body.data ?? body;
  }
  const response = await fetch(`${backendUrl}/api/v1/internal/ctfctl/${endpoint}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-ctfctl-access-key": accessKey },
    body: JSON.stringify({ scope: masterScope, ...params }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new McpEnvelopeError(responseEnvelope(body, "MCP_VALIDATION_FAILED", response.statusText));
  return body.data ?? body;
}

async function dispatch(name: string, args: Record<string, unknown>, requestId?: string | number) {
  const shortName = name.replace(/^ctfctl\./, "");
  if (!advertisedTools.has(shortName) && !compatibilityTools.has(shortName)) throw new Error("Unknown or unavailable ctfctl tool");
  const logicalToolCallId = `mcp:${masterScope.run_id}:${masterScope.attempt_id}:${masterScope.model_turn_id ?? masterScope.turn_id ?? "turn"}:${String(requestId ?? randomUUID())}`;
  const normalized = { ...args };
  if (shortName === "workspace_search" && typeof normalized.query !== "string") {
    for (const alias of ["q", "pattern", "search", "text"]) {
      if (typeof normalized[alias] === "string") {
        normalized.query = normalized[alias];
        break;
      }
    }
  }
  if (["workspace_read", "workspace_stat"].includes(shortName) && typeof normalized.path !== "string") {
    for (const alias of ["artifact_path", "file", "name"]) {
      if (typeof normalized[alias] === "string") {
        normalized.path = normalized[alias];
        break;
      }
    }
  }
  return backend(shortName, normalized, logicalToolCallId);
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
      const value = await dispatch(String(params.name ?? ""), (params.arguments ?? {}) as Record<string, unknown>, request.id);
      result = { content: [{ type: "text", text: JSON.stringify(value) }] };
    } else throw new Error(`Unsupported MCP method: ${request.method}`);
    debug("response", { id: request.id ?? null, method: request.method ?? null, ok: true });
    if (request.id !== undefined) process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    debug("response", { id: requestId ?? null, ok: false, error: message });
    if (requestId !== undefined) {
      const envelope = error instanceof McpEnvelopeError
        ? error.envelope
        : responseEnvelope({}, "MCP_VALIDATION_FAILED", message);
      process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: requestId, error: { code: -32000, message: envelope.message, data: envelope } })}\n`);
    }
  }
}
