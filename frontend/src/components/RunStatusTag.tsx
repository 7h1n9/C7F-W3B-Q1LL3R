import { Tag } from "antd";

const labels: Record<string, string> = {
  CREATED: "已创建",
  PREPARING: "准备中",
  ANALYZING: "分析中",
  PLANNING: "规划中",
  EXECUTING: "执行中",
  EVALUATING: "评估中",
  WAITING_USER: "等待确认",
  VERIFYING_FLAG: "校验 Flag",
  REPORTING: "生成报告",
  COMPLETED_SOLVED: "已解出",
  COMPLETED_UNSOLVED: "未解出",
  FAILED_ENGINE: "引擎失败",
  FAILED_TOOL: "工具失败",
  FAILED_RUNNER: "执行端失败",
  TIMEOUT: "超时",
  CANCELLED: "已取消",
  POLICY_BLOCKED: "策略拦截",
  PAUSED_RATE_LIMIT: "等待 Provider 冷却",
  RETRYING: "正在重试",
  PAUSED_CHECKPOINT: "检查点暂停",
  PAUSED_RECOVERY: "恢复暂停",
  PAUSED_DEPLOYMENT: "部署暂停",
  PAUSED_BUDGET: "预算暂停",
  WAITING_CONFIGURATION: "等待配置",
  INFRASTRUCTURE_VALIDATION: "基础设施检查",
};

function color(status: string): string {
  if (status === "COMPLETED_SOLVED") return "success";
  if (status.startsWith("FAILED") || status === "TIMEOUT") return "error";
  if (status === "CANCELLED" || status === "POLICY_BLOCKED" || status.startsWith("PAUSED")) return "default";
  if (status === "CREATED" || status === "WAITING_USER" || status === "WAITING_CONFIGURATION") return "warning";
  return "processing";
}

export function runStatusLabel(status: string): string {
  return labels[status] ?? status;
}

export function RunStatusTag({ status }: { status: string }) {
  return <Tag color={color(status)}>{runStatusLabel(status)}</Tag>;
}
