import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Codex } from "@openai/codex-sdk";
import type { ThreadEvent } from "@openai/codex-sdk";
import { threadEventsToBridgeEvents } from "./event-adapter.js";
import { ThreadStore } from "./thread-store.js";
import type { BridgeEvent, ThreadRequest, ThreadResponse } from "./types.js";

type SdkThread = { runStreamed(prompt: string): Promise<{ events: AsyncGenerator<ThreadEvent> }> };

export class CodexService {
  private readonly mock = process.env.CODEX_MOCK_MODE === "true";
  private readonly threads = new ThreadStore<SdkThread>();
  private readonly codex = this.mock ? undefined : new Codex();

  async create(input: ThreadRequest): Promise<ThreadResponse> {
    const thread = this.mock
      ? this.mockThread(input)
      : this.codex!.startThread({ workingDirectory: codexWorkingDirectory(input.workspace_path), skipGitRepoCheck: true, sandboxMode: "workspace-write", networkAccessEnabled: false });
    // The SDK only assigns its real thread id after the first turn starts. Keep
    // a bridge-local id so the backend can trigger that first turn explicitly.
    const threadId = randomUUID();
    this.threads.set(threadId, thread);
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
    const guardedPrompt = `${prompt}\n\n[EXECUTION BOUNDARY]\nDo not use direct PowerShell, shell, curl, Invoke-WebRequest, or arbitrary command execution for network or challenge analysis. Route authorized actions through the backend Tool Gateway/ctfctl interface and keep local edits inside the current Run Workspace.`;
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
    if (!this.threads.has(threadId) && !this.mock) this.threads.set(threadId, this.codex!.resumeThread(threadId) as unknown as SdkThread);
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
