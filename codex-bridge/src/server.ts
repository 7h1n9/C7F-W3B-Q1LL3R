import Fastify from "fastify";
import { CodexService } from "./codex-service.js";
import type { ThreadRequest } from "./types.js";

const app = Fastify({ logger: true });
const service = new CodexService();
function bridgeError(error: unknown) {
  const code = error instanceof Error ? error.message : "BRIDGE_ERROR";
  return { status: code === "THREAD_NOT_FOUND" ? 404 : code === "CANCEL_NOT_SUPPORTED" ? 501 : code === "MOCK_MODE_DISABLED" ? 503 : 502, body: { code, message: code === "THREAD_NOT_FOUND" ? "Thread not found" : code === "CANCEL_NOT_SUPPORTED" ? "Cancellation is not supported by the current Codex SDK" : code === "MOCK_MODE_DISABLED" ? "MOCK MODE — not executable for solving runs." : "Codex Bridge request failed", details: {} } };
}

app.get("/health", async () => service.health());
app.post<{ Body: ThreadRequest }>("/threads", async (request, reply) => {
  if (!request.body?.run_id || !request.body.workspace_path || !request.body.prompt) return reply.code(422).send({ code: "VALIDATION_ERROR", message: "run_id, workspace_path and prompt are required", details: {} });
  return service.create(request.body);
});
app.post<{ Params: { thread_id: string }; Body: { prompt: string } }>("/threads/:thread_id/run", async (request, reply) => {
  if (!service.hasThread(request.params.thread_id)) {
    return reply.code(404).send({ code: "THREAD_NOT_FOUND", message: "Thread not found", details: {} });
  }
  let hijacked = false;
  try {
    reply.hijack();
    hijacked = true;
    reply.raw.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
    for await (const event of service.stream(request.params.thread_id, request.body.prompt)) {
      reply.raw.write(`${JSON.stringify(event)}\n`);
    }
    reply.raw.end();
  } catch (error) {
    const response = bridgeError(error);
    if (hijacked) {
      reply.raw.write(`${JSON.stringify({ type: "run.failed", status: "FAILED_ENGINE", payload: { code: response.body.code, message: response.body.message } })}\n`);
      reply.raw.end();
    } else {
      return reply.code(response.status).send(response.body);
    }
  }
});
app.post<{ Params: { thread_id: string }; Body: { prompt: string } }>("/threads/:thread_id/resume", async (request, reply) => {
  if (!service.hasThread(request.params.thread_id)) {
    return reply.code(404).send({ code: "THREAD_NOT_FOUND", message: "Thread not found", details: {} });
  }
  let hijacked = false;
  try {
    reply.hijack();
    hijacked = true;
    reply.raw.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
    for await (const event of service.streamResume(request.params.thread_id, request.body.prompt)) {
      reply.raw.write(`${JSON.stringify(event)}\n`);
    }
    reply.raw.end();
  } catch (error) {
    const response = bridgeError(error);
    if (hijacked) {
      reply.raw.write(`${JSON.stringify({ type: "run.failed", status: "FAILED_ENGINE", payload: { code: response.body.code, message: response.body.message } })}\n`);
      reply.raw.end();
    } else {
      return reply.code(response.status).send(response.body);
    }
  }
});
app.post<{ Params: { thread_id: string } }>("/threads/:thread_id/cancel", async (request, reply) => {
  try { await service.cancel(request.params.thread_id); return { status: "cancelled" }; }
  catch (error) { const response = bridgeError(error); return reply.code(response.status).send(response.body); }
});

await app.listen({ port: Number(process.env.CODEX_BRIDGE_PORT ?? 8090), host: "127.0.0.1" });
