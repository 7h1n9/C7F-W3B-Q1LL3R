import type { BridgeEvent } from "./types.js";

export function finalResponseEvent(finalResponse: string): BridgeEvent {
  return { type: "agent.message", payload: { message: finalResponse } };
}
