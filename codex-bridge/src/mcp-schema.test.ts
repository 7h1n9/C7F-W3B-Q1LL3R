import assert from "node:assert/strict";
import { parametersToInputSchema, validateMcpInputSchema } from "./mcp-schema.js";

const schema = parametersToInputSchema({
  endpoint: { type: "string", required: true },
  max_requests: { type: "integer", required: false },
});

assert.deepEqual(schema, {
  type: "object",
  properties: {
    endpoint: { type: "string" },
    max_requests: { type: "integer" },
  },
  required: ["endpoint"],
  additionalProperties: false,
});

assert.deepEqual(parametersToInputSchema({}, "workspace_read"), {
  type: "object",
  additionalProperties: true,
});

assert.deepEqual(parametersToInputSchema({
  type: "object",
  properties: { path: { type: "string" } },
  required: ["path"],
}), {
  type: "object",
  properties: { path: { type: "string" } },
  required: ["path"],
});

assert.deepEqual(validateMcpInputSchema({
  type: "object",
  properties: { parameter: { type: "string" } },
  required: ["parameter"],
  additionalProperties: false,
}), []);
assert.ok(validateMcpInputSchema({ type: "object", properties: { parameter: { type: "string", required: true } } }).length > 0);
assert.ok(validateMcpInputSchema({ type: "array" }).some((item) => item.includes("items")));
