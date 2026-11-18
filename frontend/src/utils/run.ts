import type { SolverMode } from "../types/api";

export const TERMINAL_RUN_STATUSES = [
  "COMPLETED_SOLVED",
  "COMPLETED_UNSOLVED",
  "FAILED_ENGINE",
  "FAILED_TOOL",
  "FAILED_RUNNER",
  "TIMEOUT",
  "CANCELLED",
  "POLICY_BLOCKED",
] as const;

const ACTIVE_EXECUTION_RUN_STATUSES = [
  "RUNNING",
  "PREPARING",
  "ANALYZING",
  "PLANNING",
  "EXECUTING",
  "EVALUATING",
  "VERIFYING_FLAG",
  "REPORTING",
  "RETRYING",
] as const;

export function isTerminalRunStatus(status?: string | null): boolean {
  return Boolean(status && (TERMINAL_RUN_STATUSES as readonly string[]).includes(status));
}

export function isControlSourceRunStatus(status?: string | null): boolean {
  return Boolean(
    status
    && (
      (TERMINAL_RUN_STATUSES as readonly string[]).includes(status)
      || (ACTIVE_EXECUTION_RUN_STATUSES as readonly string[]).includes(status)
    ),
  );
}

export function solverModeLabel(mode?: SolverMode | string | null): string {
  if (mode === "muteki") return "Muteki（Blackboard 多 Worker）";
  if (mode === "solver_v2") return "Solver v2";
  if (mode === "multi_agent_v1") return "Multi-Agent v1";
  return "Single-Agent 兼容模式";
}
