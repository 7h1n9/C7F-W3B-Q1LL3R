import assert from "node:assert/strict";
import { threadEventsToBridgeEvents } from "./event-adapter.js";

const internalResourceProbe = threadEventsToBridgeEvents({
  type: "item.started",
  item: {
    id: "item-3",
    type: "mcp_tool_call",
    server: "codex",
    tool: "list_mcp_resources",
    arguments: {},
    status: "in_progress",
  },
} as never);

assert.deepEqual(internalResourceProbe, []);
