import type { ThreadEvent, ThreadItem } from "@openai/codex-sdk";
import type { BridgeEvent } from "./types.js";

const WAITING_USER_MARKER = /\[\[\s*C7F_WAITING_USER\s*\]\]/i;

function normalizeAgentMessage(text: string): { message: string; waitingUser: boolean } {
  const waitingUser = WAITING_USER_MARKER.test(text);
  return {
    message: text.replace(WAITING_USER_MARKER, "").trim(),
    waitingUser,
  };
}

export function threadEventsToBridgeEvents(event: ThreadEvent): BridgeEvent[] {
  if (event.type === "thread.started") {
    return [
      {
        type: "agent.message",
        payload: { message: "Codex \u7ebf\u7a0b\u5df2\u542f\u52a8", thread_id: event.thread_id },
      },
    ];
  }
  if (event.type === "turn.started") {
    return [
      {
        type: "agent.message",
        payload: { message: "Codex \u5206\u6790\u56de\u5408\u5df2\u5f00\u59cb" },
      },
    ];
  }
  if (event.type === "turn.completed") {
    return [
      {
        type: "agent.message",
        payload: { message: "Codex \u5206\u6790\u56de\u5408\u5df2\u5b8c\u6210，准备自动继续", usage: event.usage },
      },
    ];
  }
  if (event.type === "turn.failed") {
    return [
      {
        type: "run.failed",
        status: "FAILED_ENGINE",
        payload: { code: "CODEX_TURN_FAILED", message: event.error.message },
      },
    ];
  }
  if (event.type === "error") {
    return [
      {
        type: "run.failed",
        status: "FAILED_ENGINE",
        payload: { code: "CODEX_STREAM_ERROR", message: event.message },
      },
    ];
  }
  return itemEventToBridgeEvents(event.type, event.item);
}

function itemEventToBridgeEvents(
  kind: "item.started" | "item.updated" | "item.completed",
  item: ThreadItem,
): BridgeEvent[] {
  switch (item.type) {
    case "agent_message": {
      if (kind !== "item.completed") return [];
      const normalized = normalizeAgentMessage(item.text);
      return [
        {
          type: "agent.message",
          ...(normalized.waitingUser ? { status: "WAITING_USER" } : {}),
          payload: {
            message: normalized.message,
            item_id: item.id,
            requires_user_confirmation: normalized.waitingUser,
          },
        },
      ];
    }
    case "reasoning":
      return [{ type: "agent.reasoning", payload: { message: item.text, item_id: item.id } }];
    case "todo_list":
      return [{ type: "agent.plan_created", payload: { item_id: item.id, items: item.items } }];
    case "command_execution": {
      const payload = {
        tool_call_id: item.id,
        tool: "command_execution",
        status: "failed",
        error_code: "CODEX_DIRECT_TOOL_FORBIDDEN",
        error: "Codex SDK direct command execution is forbidden; use ctfctl/Tool Gateway.",
      };
      if (kind === "item.started") return [{ type: "tool.failed", status: "FAILED_ENGINE", payload }];
      return [];
    }
    case "mcp_tool_call": {
      if (item.server !== "ctfctl" && item.server !== "backend-tool-gateway") {
        return kind === "item.started" ? [{ type: "tool.failed", status: "FAILED_ENGINE", payload: { tool_call_id: item.id, tool: `${item.server}.${item.tool}`, error_code: "CODEX_DIRECT_TOOL_FORBIDDEN", error: "Only ctfctl/Backend Tool Gateway MCP tools are allowed." } }] : [];
      }
      const payload = {
        tool_call_id: item.id,
        tool: `${item.server}.${item.tool}`,
        server: item.server,
        arguments: item.arguments,
        result: item.result,
        error: item.error,
        status: item.status,
      };
      if (kind === "item.started") return [{ type: "tool.started", payload }];
      if (kind === "item.updated") return [{ type: "tool.output", payload }];
      return [{ type: item.status === "completed" ? "tool.completed" : "tool.failed", payload }];
    }
    case "web_search": {
      return kind === "item.started" ? [{ type: "tool.failed", status: "FAILED_ENGINE", payload: { tool_call_id: item.id, tool: "web_search", error_code: "CODEX_DIRECT_TOOL_FORBIDDEN", error: "Use ctfctl/Tool Gateway instead." } }] : [];
    }
    case "file_change":
      return kind === "item.completed"
        ? [{ type: "artifact.created", payload: { artifact_id: item.id, changes: item.changes, status: item.status } }]
        : [];
    case "error":
      return [
        {
          type: "run.failed",
          status: "FAILED_ENGINE",
          payload: { code: "CODEX_ITEM_ERROR", message: item.message, item_id: item.id },
        },
      ];
    default:
      return [];
  }
}
