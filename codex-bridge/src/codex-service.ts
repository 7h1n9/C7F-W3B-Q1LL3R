import { randomUUID } from "node:crypto";
import { Codex } from "@openai/codex-sdk";
import { finalResponseEvent } from "./event-adapter.js";
import { ThreadStore } from "./thread-store.js";
import type { BridgeEvent, ThreadRequest, ThreadResponse } from "./types.js";

type SdkThread = { run(prompt: string): Promise<{ finalResponse: string }> };
type IdentifiedThread = SdkThread & { id: string | null };

export class CodexService {
  private readonly mock = process.env.CODEX_MOCK_MODE === "true";
  private readonly threads = new ThreadStore<SdkThread>();
  private readonly initialEvents = new Map<string, BridgeEvent[]>();
  private readonly codex = this.mock ? undefined : new Codex();

  async create(input: ThreadRequest): Promise<ThreadResponse> {
    const thread = this.mock
      ? this.mockThread(input)
      : (this.codex!.startThread({ workingDirectory: input.workspace_path, skipGitRepoCheck: true, sandboxMode: "workspace-write", networkAccessEnabled: false }) as unknown as IdentifiedThread);
    const initial = await thread.run(input.prompt);
    const threadId = this.mock ? randomUUID() : (thread as IdentifiedThread).id;
    if (!threadId) throw new Error("CODEX_THREAD_ID_UNAVAILABLE");
    this.threads.set(threadId, thread);
    this.threads.mapRun(input.run_id, threadId);
    this.initialEvents.set(threadId, [finalResponseEvent(initial.finalResponse)]);
    return { thread_id: threadId, status: "created" };
  }

  async run(threadId: string, prompt: string): Promise<BridgeEvent[]> {
    const thread = this.threads.get(threadId);
    if (!thread) throw new Error("THREAD_NOT_FOUND");
    const initial = this.initialEvents.get(threadId) ?? [];
    this.initialEvents.delete(threadId);
    const result = await thread.run(prompt);
    return [...initial, finalResponseEvent(result.finalResponse)];
  }

  async resume(threadId: string, prompt: string): Promise<BridgeEvent[]> {
    if (!this.threads.has(threadId) && !this.mock) this.threads.set(threadId, this.codex!.resumeThread(threadId) as unknown as SdkThread);
    return this.run(threadId, prompt);
  }

  async cancel(threadId: string): Promise<void> {
    if (!this.threads.has(threadId)) throw new Error("THREAD_NOT_FOUND");
    // The current SDK exposes no cancellation primitive. Do not claim success.
    throw new Error("CANCEL_NOT_SUPPORTED");
  }

  private mockThread(input: ThreadRequest): SdkThread {
    return { run: async (prompt: string) => ({ finalResponse: `[mock] Authorized workspace ${input.workspace_path}: ${prompt}` }) };
  }
}
