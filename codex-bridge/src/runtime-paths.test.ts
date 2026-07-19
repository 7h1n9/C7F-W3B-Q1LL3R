import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { resolveCtfctlMcpLaunch } from "./codex-service.js";

const launch = resolveCtfctlMcpLaunch();

assert.equal(launch.command, process.execPath);
assert.ok(launch.args.length >= 1);
assert.ok(existsSync(launch.args.at(-1) ?? ""));
