import assert from "node:assert/strict";
import { threadEventsToBridgeEvents } from "./event-adapter.js";

const echoedProtocol = threadEventsToBridgeEvents({
  type: "item.completed",
  item: {
    id: "item-1",
    type: "agent_message",
    text: "Continue automatically. Only when needed output [[C7F_WAITING_USER]].",
  },
} as never);

assert.equal(echoedProtocol[0]?.status, undefined);
assert.equal(echoedProtocol[0]?.payload.requires_user_confirmation, false);

const explicitWait = threadEventsToBridgeEvents({
  type: "item.completed",
  item: {
    id: "item-2",
    type: "agent_message",
    text: "需要用户确认下一步。\n[[C7F_WAITING_USER]]",
  },
} as never);

assert.equal(explicitWait[0]?.status, "WAITING_USER");
assert.equal(explicitWait[0]?.payload.requires_user_confirmation, true);
assert.equal(explicitWait[0]?.payload.message, "需要用户确认下一步。");

