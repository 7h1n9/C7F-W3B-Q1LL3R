export class ThreadStore<T = unknown> {
  private readonly threads = new Map<string, T>();
  private readonly runThreads = new Map<string, string>();
  set(id: string, thread: T): void { this.threads.set(id, thread); }
  get(id: string): T | undefined { return this.threads.get(id); }
  has(id: string): boolean { return this.threads.has(id); }
  mapRun(runId: string, threadId: string): void { this.runThreads.set(runId, threadId); }
  getForRun(runId: string): string | undefined { return this.runThreads.get(runId); }
}
