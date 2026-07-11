import Fastify from "fastify";
import { CodexService } from "./codex-service.js";
import type { ThreadRequest } from "./types.js";

const app = Fastify({ logger: true });
const service = new CodexService();
function bridgeError(error: unknown) {
  const code = error instanceof Error ? error.message : "BRIDGE_ERROR";
  return { status: code === "THREAD_NOT_FOUND" ? 404 : code === "CANCEL_NOT_SUPPORTED" ? 501 : 502, body: { code, message: code === "THREAD_NOT_FOUND" ? "Thread not found" : code === "CANCEL_NOT_SUPPORTED" ? "Cancellation is not supported by the current Codex SDK" : "Codex Bridge request failed", details: {} } };
}

app.get("/health", async () => ({ status: "ok", mock_mode: process.env.CODEX_MOCK_MODE === "true" }));
app.post<{ Body: ThreadRequest }>("/threads", async (request, reply) => {
  if (!request.body?.run_id || !request.body.workspace_path || !request.body.prompt) return reply.code(422).send({ code: "VALIDATION_ERROR", message: "run_id, workspace_path and prompt are required", details: {} });
  return service.create(request.body);
});
app.post<{ Params: { thread_id: string }; Body: { prompt: string } }>("/threads/:thread_id/run", async (request, reply) => {
  try { return { events: await service.run(request.params.thread_id, request.body.prompt) }; }
  catch (error) { const response = bridgeError(error); return reply.code(response.status).send(response.body); }
});
app.post<{ Params: { thread_id: string }; Body: { prompt: string } }>("/threads/:thread_id/resume", async (request, reply) => {
  try { return { events: await service.resume(request.params.thread_id, request.body.prompt) }; }
  catch (error) { const response = bridgeError(error); return reply.code(response.status).send(response.body); }
});
app.post<{ Params: { thread_id: string } }>("/threads/:thread_id/cancel", async (request, reply) => {
  try { await service.cancel(request.params.thread_id); return { status: "cancelled" }; }
  catch (error) { const response = bridgeError(error); return reply.code(response.status).send(response.body); }
});

await app.listen({ port: Number(process.env.CODEX_BRIDGE_PORT ?? 8090), host: "127.0.0.1" });
