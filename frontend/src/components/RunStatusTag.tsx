import { Tag } from "antd";

const labels: Record<string, string> = {
  CREATED: "已创建", PREPARING: "准备中", ANALYZING: "分析中", PLANNING: "规划中",
  EXECUTING: "执行中", EVALUATING: "评估中", WAITING_USER: "等待确认", VERIFYING_FLAG: "校验 Flag",
  REPORTING: "生成报告", COMPLETED_SOLVED: "已解出", COMPLETED_UNSOLVED: "未解出",
  FAILED_ENGINE: "引擎失败", FAILED_TOOL: "工具失败", FAILED_RUNNER: "执行端失败",
  TIMEOUT: "超时", CANCELLED: "已取消", POLICY_BLOCKED: "策略拦截", PAUSED_RATE_LIMIT: "等待 Provider 冷却",
};

function color(status: string): string {
  if (status === "COMPLETED_SOLVED") return "success";
  if (status.startsWith("FAILED") || status === "TIMEOUT") return "error";
  if (status === "CANCELLED" || status === "POLICY_BLOCKED") return "default";
  if (status === "CREATED" || status === "WAITING_USER") return "warning";
  return "processing";
}

export function runStatusLabel(status: string): string {
  return labels[status] ?? status;
}

export function RunStatusTag({ status }: { status: string }) {
  return <Tag color={color(status)}>{runStatusLabel(status)}</Tag>;
}
