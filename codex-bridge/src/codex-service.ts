import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, symlinkSync } from "node:fs";
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
    return {
      status: "ok",
      mock_mode: this.mock,
      executable: !this.mock,
      codex_sdk_loaded: true,
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
    const mcpProgram = resolve(fileURLToPath(new URL("./ctfctl-mcp.js", import.meta.url)));
    // SDK 0.144.1 exposes MCP registration through Codex.config, which it
    // serializes into native CLI configuration. The scope is supplied as
    // subprocess environment, never through model-visible prompt text.
    const codex = new Codex({
      config: {
        mcp_servers: {
          ctfctl: {
            command: process.execPath,
            args: [mcpProgram],
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

type CtfctlScope = { run_id: string; challenge_id: string; workspace_root: string; allowed_hosts: string[]; attempt_id: string; lease_token: string; thread_id?: string; model_turn_id?: string };

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
