import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Codex } from "@openai/codex-sdk";
import type { ThreadEvent } from "@openai/codex-sdk";
import { threadEventsToBridgeEvents } from "./event-adapter.js";
import { ThreadStore } from "./thread-store.js";
import type { BridgeEvent, ThreadRequest, ThreadResponse } from "./types.js";

type SdkThread = { runStreamed(prompt: string): Promise<{ events: AsyncGenerator<ThreadEvent> }> };

export class CodexService {
  private readonly mock = process.env.CODEX_MOCK_MODE === "true";
  private readonly threads = new ThreadStore<SdkThread>();
  private readonly scopes = new Map<string, CtfctlScope>();

  health(): Record<string, unknown> {
    let ctfctlMcpReady = true;
    try {
      resolveCtfctlMcpLaunch();
    } catch {
      ctfctlMcpReady = false;
    }
    return {
      status: "ok",
      mock_mode: this.mock,
      executable: !this.mock,
      codex_sdk_loaded: true,
      sdk_version: sdkVersion(),
      ctfctl_mcp_ready: ctfctlMcpReady,
      version: bridgeVersion(),
      active_threads: this.threads.size(),
    };
  }

  hasThread(threadId: string): boolean {
    return this.threads.has(threadId);
  }

  async create(input: ThreadRequest): Promise<ThreadResponse> {
    if (this.mock && process.env.NODE_ENV !== "test") {
      throw new Error("MOCK_MODE_DISABLED");
    }
    const thread = this.mock
      ? this.mockThread(input)
      : this.createRealThread(input);
    // The SDK only assigns its real thread id after the first turn starts. Keep
    // a bridge-local id so the backend can trigger that first turn explicitly.
    const threadId = randomUUID();
    this.threads.set(threadId, thread);
    this.scopes.set(threadId, normalizeScope(input));
    this.threads.mapRun(input.run_id, threadId);
    return { thread_id: threadId, status: "created" };
  }

  async run(threadId: string, prompt: string): Promise<BridgeEvent[]> {
    const events: BridgeEvent[] = [];
    for await (const event of this.stream(threadId, prompt)) events.push(event);
    return events;
  }

  async *stream(threadId: string, prompt: string): AsyncGenerator<BridgeEvent> {
    const thread = this.threads.get(threadId);
    if (!thread) throw new Error("THREAD_NOT_FOUND");
    const guardedPrompt = `${prompt}\n\n[EXECUTION BOUNDARY]\nUse the installed ctfctl MCP tools exclusively for workspace and target operations. Do not use direct shell, command_execution, node repl, web_search, curl, Invoke-WebRequest, localhost/backend API calls, source-code browsing outside the current Run Workspace, or create other Runs. ctfctl.workspace_write_note may only edit notes/, scripts/, and final/.`;
    const streamed = await thread.runStreamed(guardedPrompt);
    for await (const event of streamed.events) {
      for (const bridgeEvent of threadEventsToBridgeEvents(event)) yield bridgeEvent;
    }
  }

  async resume(threadId: string, prompt: string): Promise<BridgeEvent[]> {
    const events: BridgeEvent[] = [];
    for await (const event of this.streamResume(threadId, prompt)) events.push(event);
    return events;
  }

  async *streamResume(threadId: string, prompt: string): AsyncGenerator<BridgeEvent> {
    if (!this.threads.has(threadId) && !this.mock) throw new Error("THREAD_NOT_FOUND");
    for await (const event of this.stream(threadId, prompt)) yield event;
  }

  async cancel(threadId: string): Promise<void> {
    if (!this.threads.has(threadId)) throw new Error("THREAD_NOT_FOUND");
    // The current SDK exposes no cancellation primitive. Do not claim success.
    throw new Error("CANCEL_NOT_SUPPORTED");
  }

  failureResponse(threadId: string, error: unknown): { code: string; message: string; details: Record<string, unknown> } {
    const raw = error instanceof Error ? error.message : String(error);
    const lower = raw.toLowerCase();
    const code = lower.includes("thread/start failed") || lower.includes("failed to create thread")
      ? "CODEX_THREAD_CREATE_FAILED"
      : lower.includes("ctfctl_") || lower.includes("mcp error")
      ? "MCP_BACKEND_UNREACHABLE"
      : lower.includes("required mcp") || lower.includes("initialize")
        ? "MCP_INITIALIZE_FAILED"
        : lower.includes("spawn") || lower.includes("enoent") || lower.includes("process start")
          ? "MCP_PROCESS_START_FAILED"
          : lower.includes("tool catalog") || lower.includes("tools/list")
            ? "MCP_TOOL_CATALOG_FAILED"
            : lower.includes("codex exec exited")
              ? "CODEX_CLI_EXITED"
              : "CODEX_STREAM_INTERRUPTED";
    const diagnosticId = randomUUID();
    const scope = this.scopes.get(threadId);
    const safe = raw.replace(/(access[_ -]?key|lease[_ -]?token|tool[_ -]?ticket|cookie|api[_ -]?key)=?[^\s,;]+/gi, "$1=[REDACTED]");
    const diagnostic = {
      diagnostic_id: diagnosticId,
      exit_code: Number(raw.match(/exited with code (\d+)/i)?.[1] ?? 0),
      stderr: safe.slice(0, 12000), stdout_excerpt: "", sdk_version: sdkVersion(),
      node_version: process.version, command: process.env.CODEX_PATH ?? "codex",
      args: ["exec", "--experimental-json"],
      environment_key_names: Object.keys(process.env).filter((key) => /CODEX|MCP|NODE_ENV/.test(key)).sort(),
      failed_stage: code.startsWith("MCP_") ? "MCP_INITIALIZE" : "CODEX_STREAM",
      timestamp: new Date().toISOString(),
    };
    let diagnosticPath: string | undefined;
    if (scope?.workspace_root) {
      try {
        const directory = join(resolve(scope.workspace_root), "diagnostics", "codex-bridge");
        mkdirSync(directory, { recursive: true });
        diagnosticPath = join(directory, `${diagnosticId}.json`);
        writeFileSync(diagnosticPath, JSON.stringify(diagnostic, null, 2), "utf8");
      } catch { /* diagnostics must never mask the stable error */ }
    }
    return { code, message: code.startsWith("MCP_") ? "ctfctl MCP failed during startup." : "Codex Bridge stream failed.", details: { diagnostic_id: diagnosticId, diagnostic_artifact: diagnosticPath, stage: diagnostic.failed_stage } };
  }

  private mockThread(input: ThreadRequest): SdkThread {
    return {
      runStreamed: async (prompt: string) => ({
        events: (async function* (): AsyncGenerator<ThreadEvent> {
          yield { type: "turn.started" };
          yield { type: "item.completed", item: { id: randomUUID(), type: "agent_message", text: `[mock] Authorized workspace ${input.workspace_path}: ${prompt}` } };
          yield { type: "turn.completed", usage: { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, reasoning_output_tokens: 0 } };
        })(),
      }),
    };
  }

  private createRealThread(input: ThreadRequest): SdkThread {
    const scope = normalizeScope(input);
    const mcpLaunch = resolveCtfctlMcpLaunch();
    // SDK 0.144.1 exposes MCP registration through Codex.config, which it
    // serializes into native CLI configuration. The scope is supplied as
    // subprocess environment, never through model-visible prompt text.
    const codex = new Codex({
      config: {
        mcp_servers: {
          ctfctl: {
            enabled: true,
            required: Boolean(scope.mcp_required ?? (process.env.CODEX_MCP_REQUIRED === "true")),
            command: mcpLaunch.command,
            args: mcpLaunch.args,
            env: {
              CTFCTL_SCOPE: JSON.stringify(scope),
              CTFCTL_BACKEND_URL: process.env.CTFCTL_BACKEND_URL ?? "http://127.0.0.1:8000",
              CTFCTL_ACCESS_KEY: process.env.CTFCTL_ACCESS_KEY ?? "development-ctfctl-access-key",
              ...(process.env.CTFCTL_DEBUG_LOG ? { CTFCTL_DEBUG_LOG: process.env.CTFCTL_DEBUG_LOG } : {}),
            },
            startup_timeout_sec: 120,
          },
        },
      },
    });
    return codex.startThread({
      model: process.env.CODEX_MODEL ?? "gpt-5.6-luna",
      workingDirectory: codexWorkingDirectory(input.workspace_path),
      skipGitRepoCheck: true,
      sandboxMode: "workspace-write",
      // The run-scoped ctfctl stdio server calls the local Backend Tool Gateway.
      // Denying all network access cancels that MCP call before the server can
      // reach the gateway. Target access remains constrained by the backend's
      // challenge allowlist and the Kali Runner.
      networkAccessEnabled: true,
      webSearchMode: "disabled",
      approvalPolicy: "never",
    }) as unknown as SdkThread;
  }
}

export function resolveCtfctlMcpLaunch(): { command: string; args: string[] } {
  const compiledProgram = resolve(
    fileURLToPath(new URL("./ctfctl-mcp.js", import.meta.url)),
  );
  if (existsSync(compiledProgram)) {
    return { command: process.execPath, args: [compiledProgram] };
  }

  // `npm run dev` executes this file directly from src/ through tsx. In that
  // mode the sibling JavaScript file does not exist, so launch the TypeScript
  // MCP entrypoint through the installed tsx CLI instead of silently creating
  // a Codex thread with no usable ctfctl server.
  const sourceProgram = resolve(
    fileURLToPath(new URL("./ctfctl-mcp.ts", import.meta.url)),
  );
  if (existsSync(sourceProgram)) {
    const require = createRequire(import.meta.url);
    const tsxCli = require.resolve("tsx/cli");
    return { command: process.execPath, args: [tsxCli, sourceProgram] };
  }

  throw new Error("CTFCTL_MCP_ENTRYPOINT_NOT_FOUND");
}

type CtfctlScope = { run_id: string; challenge_id: string; workspace_root: string; allowed_hosts: string[]; attempt_id: string; lease_token: string; master_lease_token?: string; mcp_required?: boolean; thread_id?: string; model_turn_id?: string };

function normalizeScope(input: ThreadRequest): CtfctlScope {
  const raw = input.scope ?? {};
  return {
    run_id: input.run_id,
    challenge_id: typeof raw.challenge_id === "string" ? raw.challenge_id : "",
    workspace_root: input.workspace_path,
    allowed_hosts: Array.isArray(raw.allowed_hosts)
      ? raw.allowed_hosts.filter((item): item is string => typeof item === "string")
      : [],
    attempt_id: typeof raw.attempt_id === "string" ? raw.attempt_id : "",
    lease_token: typeof raw.lease_token === "string" ? raw.lease_token : "",
    master_lease_token: typeof raw.master_lease_token === "string" ? raw.master_lease_token : undefined,
    mcp_required: typeof raw.mcp_required === "boolean" ? raw.mcp_required : undefined,
    thread_id: typeof raw.thread_id === "string" ? raw.thread_id : undefined,
    model_turn_id: typeof raw.model_turn_id === "string" ? raw.model_turn_id : undefined,
  };
}

function bridgeVersion(): string {
  try {
    const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf-8"));
    return String(packageJson.version || "unknown");
  } catch {
    return "unknown";
  }
}

function sdkVersion(): string {
  try {
    const packageJson = JSON.parse(readFileSync(new URL("../node_modules/@openai/codex-sdk/package.json", import.meta.url), "utf-8"));
    return String(packageJson.version || "unknown");
  } catch {
    return "unknown";
  }
}

function codexWorkingDirectory(workspacePath: string): string {
  if (process.platform !== "win32" || /^[\x00-\x7f]*$/.test(workspacePath)) return workspacePath;
  try {
    const aliasRoot = join(tmpdir(), "c7f-codex-workspaces");
    mkdirSync(aliasRoot, { recursive: true });
    const alias = join(aliasRoot, randomUUID());
    if (!existsSync(alias)) symlinkSync(workspacePath, alias, "junction");
    return alias;
  } catch {
    // Fall back to an 8.3 path if junction creation is unavailable.
  }
  try {
    const shortPath = execFileSync(
      "cmd.exe",
      ["/d", "/u", "/c", `for %I in (${workspacePath}) do @echo %~sI`],
      { encoding: null, windowsHide: true },
    ).toString("utf16le").trim();
    return shortPath || workspacePath;
  } catch {
    return workspacePath;
  }
}
