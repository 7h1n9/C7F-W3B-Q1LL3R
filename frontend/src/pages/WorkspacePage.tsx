import {
  CaretRightOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  CompassOutlined,
  DownloadOutlined,
  EyeOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  FlagFilled,
  FullscreenExitOutlined,
  FullscreenOutlined,
  NodeIndexOutlined,
  PauseCircleOutlined,
  PushpinOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  ThunderboltOutlined,
  UserOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Badge, Button, Card, Empty, Input, Modal, Space, Statistic, Table, Tabs, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { useParams } from "react-router-dom";
import { RunStatusTag } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { FlagCandidate, MutekiBoardSemantic, MutekiGraphState, RunEvent, RunUserInput } from "../types/api";
import { isTerminalRunStatus, solverModeLabel } from "../utils/run";

type EventPayload = Record<string, unknown>;
type NormalizedEvent = { type: string; actor: string; payload: EventPayload; verified: boolean };
/** One published ``worker_command``: the only record of what an agent ran. */
type WorkerCommandRecord = { sequence: number; command: string; result: string; at: string };
type WorkerState = {
  id: string;
  role: string;
  engine: string;
  status: "running" | "finished";
  intentId?: string;
  branchId?: string;
  startedAt?: string;
  finishedAt?: string;
  /** Last event timestamp for this worker; silence is the "frozen" signal. */
  lastSeenAt?: string;
  steps: number;
  toolSteps: number;
  distinctTools: string[];
  /** Bounded tail of the agent's action trail (newest last). */
  commands: WorkerCommandRecord[];
  commandCount: number;
  factCount: number;
  resultCode?: string;
};
type IntentState = { id: string; description: string; status: "open" | "claimed" | "done"; worker?: string; tool?: string; classification?: string; result?: string };
type FactState = { sequence: number; content: string; verified: boolean; evidenceCount: number; state?: "unresolved" | "challenged" | "revalidated" };
type BoardState = { phase: string; flagFound: boolean; workers: WorkerState[]; intents: IntentState[]; facts: FactState[]; reviewCount: number };
type CaseBoardCard = {
  id: string;
  kind: "fact" | "finding" | "flag";
  content: string;
  /** Secondary line: Codex/board semantic summary. Never replaces ``content``. */
  summary?: string;
  sourceContent?: string;
  verified: boolean;
  evidenceRefs: string[];
  source?: string;
  sequence: number;
};
type CaseBoardSections = { confirmed: CaseBoardCard[]; pending: CaseBoardCard[] };
type BoardPosition = { x: number; y: number };
type BoardLayout = { runId: string; positions: Record<string, BoardPosition> };
type WorkspaceMoment = { id: string; kind: "system" | "user"; title: string; summary: string; timestamp: string; tone: "success" | "danger" | "active" | "quiet"; sequence?: number; status?: string };
type UsageState = { costUsd?: number; tokens?: number; inputTokens?: number; outputTokens?: number; calls?: number };

const keyEventTypes = new Set([
  "run.created", "run.started", "run.completed", "run.failed", "flag.verified",
  "solver.run.started", "solver.completion.evaluated", "solver.run.completed", "solver.run.failed",
  "solver.action.planned", "solver.action.authorized", "solver.action.started", "solver.action.completed",
  "solver.action.failed", "solver.action.interrupted", "solver.action.recovered", "solver.tool.called",
  "solver.observation.received", "solver.step.completed",
  "muteki.run.started", "muteki.run_finished", "muteki.phase_changed", "muteki.fact_added",
  "muteki.prepare.engine.checked", "muteki.dead_end", "muteki.intent_proposed", "muteki.intent_claimed",
  "muteki.intent_released", "muteki.intent_concluded", "muteki.flag_candidate", "muteki.flag_found",
  "muteki.worker_started", "muteki.worker_step", "muteki.worker_finished", "muteki.review_finding",
  "muteki.review_proposal", "muteki.review_proposal_decision", "muteki.coordinator_directive",
  "muteki.operator_directive", "muteki.intent_state_changed", "muteki.run.titled",
]);

const eventLabels: Record<string, string> = {
  "muteki.run.titled": "解题标题",
  "run.created": "任务创建",
  "run.started": "任务启动",
  "run.completed": "任务完成",
  "run.failed": "任务失败",
  "solver.run.started": "Solver 启动",
  "solver.run.completed": "Solver 完成",
  "solver.run.failed": "Solver 失败",
  "solver.action.planned": "Action 规划",
  "solver.action.authorized": "Action 授权",
  "solver.action.started": "Action 执行",
  "solver.action.completed": "Action 完成",
  "solver.action.failed": "Action 失败",
  "solver.action.interrupted": "Action 中断",
  "solver.action.recovered": "Action 恢复",
  "solver.tool.called": "Tool Gateway 调用",
  "solver.observation.received": "Observation 接收",
  "solver.completion.evaluated": "Completion Gate 判定",
  "solver.step.completed": "Solver Step 完成",
  "tool.requested": "工具请求",
  "tool.started": "工具启动",
  "tool.completed": "工具完成",
  "tool.failed": "工具失败",
  "artifact.created": "证据保存",
  "flag.verified": "Flag 已验证",
  "muteki.run.started": "Muteki 启动",
  "muteki.run_finished": "Muteki 结束",
  "muteki.phase_changed": "阶段切换",
  "muteki.fact_added": "黑板写入事实",
  "muteki.dead_end": "记录死路",
  "muteki.intent_proposed": "提出 Intent",
  "muteki.intent_claimed": "Worker 认领 Intent",
  "muteki.intent_released": "Intent 释放",
  "muteki.intent_concluded": "Intent 完成",
  "muteki.worker_started": "Worker 启动",
  "muteki.worker_step": "Worker 进度",
  "muteki.worker_finished": "Worker 结束",
  "muteki.review_finding": "Review 发现",
  "muteki.review_proposal": "Review 提案",
  "muteki.review_proposal_decision": "Review 决策",
  "muteki.branch_split": "分支拆分",
  "muteki.branch_resolved": "分支收敛",
  "muteki.route_suppressed": "路由抑制",
  "muteki.route_reopened": "路由恢复",
  "muteki.flag_found": "Flag Gate 通过",
  "muteki.prepare.engine.checked": "引擎探活",
  "muteki.coordinator_directive": "Coordinator 调度",
  "muteki.operator_directive": "Operator 指令",
  "muteki.intent_state_changed": "Intent 状态变化",
};

const stages = [
  { key: "prepare", label: "Prepare", note: "初始化" },
  { key: "race", label: "Race", note: "侦察" },
  { key: "coordinator", label: "Coordinator", note: "协调执行" },
  { key: "finalize", label: "Finalize", note: "验证关闭" },
];

const terminalStatuses = ["COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "FAILED_ENGINE", "FAILED_TOOL", "FAILED_RUNNER", "TIMEOUT", "CANCELLED", "POLICY_BLOCKED"];
const startableRunStatuses = ["CREATED", "PAUSED_RECOVERY", "PAUSED_DEPLOYMENT", "PAUSED_CHECKPOINT", "WAITING_CONFIGURATION"];
const restartableRunStatuses = ["WAITING_USER", "FAILED_ENGINE", "FAILED_TOOL", "FAILED_RUNNER", "TIMEOUT", "COMPLETED_UNSOLVED", "CANCELLED", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY", "PAUSED_DEPLOYMENT", "WAITING_CONFIGURATION"];

function isRecord(value: unknown): value is EventPayload {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

// A very long payload (multi-KB event body / fact) keeps only a generous
// preview in the DOM while collapsed so a dense list cannot be bloated by the
// full strings; the toggle always reports the true length and expanding swaps
// in the complete value.
const CLAMP_PREVIEW_LIMIT = 2000;

// Collapsed prose is *measured*, never sliced: the toggle only appears when the
// rendered text really overflows the clamped box, so short rows stay clean.
function ExpandableText({ value, lines = 3, className = "", toggleClassName = "muteki-expand-toggle" }: {
  value: string;
  lines?: number;
  className?: string;
  toggleClassName?: string;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  const preview = value.length > CLAMP_PREVIEW_LIMIT ? value.slice(0, CLAMP_PREVIEW_LIMIT) : value;
  const hiddenByPreview = value.length > preview.length;

  useEffect(() => {
    setExpanded(false);
  }, [value]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || expanded) return;
    const frame = window.requestAnimationFrame(() => {
      const collapsedHeight = body.clientHeight;
      const display = body.style.display;
      // `display:block` disables -webkit-line-clamp, so scrollHeight is the full
      // text height. The clamp value is a React-owned inline style and must stay
      // untouched, otherwise the collapsed state would forget it.
      body.style.display = "block";
      const fullHeight = body.scrollHeight;
      body.style.display = display;
      setClamped(fullHeight > collapsedHeight + 1);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [preview, expanded, lines]);

  return (
    <div className="muteki-expandable">
      <div
        ref={bodyRef}
        className={`muteki-expandable-body ${expanded ? "is-expanded" : ""} ${className}`.trim()}
        style={expanded ? undefined : { WebkitLineClamp: lines }}
      >
        {expanded ? value : preview}
      </div>
      {(clamped || hiddenByPreview || expanded) && (
        <button type="button" className={toggleClassName} onClick={() => setExpanded((current) => !current)}>
          {expanded ? "收起" : `展开全文 · ${value.trim().length} 字`}
        </button>
      )}
    </div>
  );
}

function defaultBoardPosition(index: number): BoardPosition {
  const column = index % 4;
  const row = Math.floor(index / 4);
  return { x: 2 + column * 24, y: 3 + row * 29 };
}

function readBoardLayout(runId: string): BoardLayout {
  if (!runId || typeof window === "undefined") return { runId, positions: {} };
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(`muteki-board-layout:${runId}`) ?? "{}");
    if (!isRecord(parsed) || !isRecord(parsed.positions)) return { runId, positions: {} };
    const positions: Record<string, BoardPosition> = {};
    for (const [cardId, value] of Object.entries(parsed.positions)) {
      if (!isRecord(value)) continue;
      const x = Number(value.x);
      const y = Number(value.y);
      if (Number.isFinite(x) && Number.isFinite(y)) positions[cardId] = { x: Math.max(0, Math.min(94, x)), y: Math.max(0, Math.min(88, y)) };
    }
    return { runId, positions };
  } catch {
    return { runId, positions: {} };
  }
}

function normalizeEvent(event: RunEvent): NormalizedEvent {
  const root = isRecord(event.payload_json) ? event.payload_json : {};
  const payload = isRecord(root.payload) ? root.payload : root;
  return {
    type: text(root.muteki_event_type, event.event_type.replace(/^muteki\./, "")),
    actor: text(root.actor, "system"),
    payload,
    verified: Boolean(root.verified),
  };
}

function eventTone(type: string): "success" | "danger" | "active" | "quiet" {
  if (type.includes("failed") || type.includes("dead_end") || type.includes("rejected")) return "danger";
  if (type.includes("flag") || type.includes("verified") || type.includes("completed") || type.includes("concluded")) return "success";
  if (type.includes("started") || type.includes("claimed") || type.includes("proposed") || type.includes("requested")) return "active";
  return "quiet";
}

function safeEventSummary(event: RunEvent): string {
  const { type, actor, payload } = normalizeEvent(event);
  const tool = text(payload.tool, text(payload.tool_name));
  if (type.startsWith("solver.")) {
    const action = text(payload.action_name, text(payload.action, tool));
    const reason = text(payload.reason_code, text(payload.reason));
    const status = text(payload.status, text(payload.decision));
    return [action || type, status, reason].filter(Boolean).join(" · ");
  }
  if (type === "phase_changed") return `进入 ${text(payload.phase, "未知阶段").toUpperCase()}`;
  if (type === "worker_started" || type === "worker_finished" || type === "worker_step") return `${text(payload.role, "worker")} · ${text(payload.engine_id, "engine")} · ${actor}`;
  if (type === "intent_proposed") return text(payload.description, "新 Intent");
  if (type === "intent_claimed" || type === "intent_released" || type === "intent_concluded") return `${text(payload.intent_id, "intent")} ${text(payload.result, "")}`.trim();
  if (type === "fact_added") return text(payload.content, "写入一条事实");
  if (type === "coordinator_directive" || type === "operator_directive" || type === "intent_state_changed") {
    return [text(payload.directive, text(payload.state, type)), text(payload.reason_code, text(payload.reason))].filter(Boolean).join(" · ");
  }
  if (type === "tool.requested" || type === "tool.started" || type === "tool.completed" || type === "tool.failed") {
    const fallbackStatus = type.split(".")[type.split(".").length - 1] ?? "更新";
    return `${tool || "tool"} · ${text(payload.status, fallbackStatus)}`;
  }
  if (type === "artifact.created") return `${text(payload.path, "artifact")} · ${text(payload.size, "")}`.trim();
  if (type === "review_finding" || type === "review_proposal" || type === "review_proposal_decision") return text(payload.reason ?? payload.label ?? payload.decision, "Review 更新");
  if (type === "branch_split" || type === "branch_resolved" || type === "route_suppressed" || type === "route_reopened") return text(payload.reason ?? payload.route_hash ?? type);
  if (type === "flag_found") return "证据已通过 Flag Gate";
  return `${actor} · ${eventLabels[event.event_type] ?? event.event_type}`;
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return undefined;
}

function verifiedCanonicalFlag(
  runStatus: string | undefined,
  reportJson?: Record<string, unknown>,
  flagPattern?: string,
): string | undefined {
  if (runStatus !== "COMPLETED_SOLVED" || reportJson?.flag_verified !== true) return undefined;
  const value = reportJson.flag;
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  // The banner is per-challenge: CTF2{...}, DASCTF{...}, flag{...}. Testing a
  // hardcoded flag{...} hid every other banner behind "FLAG EVENT RECORDED"
  // even though the Run was solved. Prefer the challenge's own pattern and fall
  // back to the generic banner{body} shape the backend also accepts.
  return matchesFlagPattern(text, flagPattern) ? text : undefined;
}

function matchesFlagPattern(value: string, flagPattern?: string): boolean {
  const generic = /^[A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*\{[^{}\r\n"'\\]{1,256}\}$/;
  if (flagPattern) {
    try {
      // Anchored so a permissive ``[^}]+`` cannot match a substring.
      if (new RegExp(`^(?:${flagPattern})$`).test(value)) return true;
    } catch {
      /* an unusable pattern falls through to the generic shape */
    }
  }
  if (!generic.test(value) && !/^flag\{[^{}\r\n"'\\]{1,256}\}$/i.test(value)) return false;
  // Mirrors the backend's placeholder policy: ``flag{...}`` / ``DASCTF{placeholder}``
  // are "put a flag here" prose, never a result.
  const body = value.slice(value.indexOf("{") + 1, -1).trim().toLowerCase();
  return !["...", "…", "xxx", "xxxx", "placeholder", "redacted", "todo", "tbd", "your_flag", "the flag", "flag"].includes(body);
}

function projectKeyMoments(events: RunEvent[], inputs: RunUserInput[]): WorkspaceMoment[] {
  const systemMoments = events.flatMap((event) => {
    if (!keyEventTypes.has(event.event_type)) return [];
    const { type, payload, verified } = normalizeEvent(event);
    if (type === "fact_added" && (!Boolean(payload.verified ?? verified) || text(payload.content).startsWith("REVIEW_NEEDED"))) return [];
    return [{
      id: `event-${event.sequence}`,
      kind: "system" as const,
      title: eventLabels[event.event_type] ?? event.event_type,
      summary: safeEventSummary(event),
      timestamp: event.created_at,
      tone: eventTone(event.event_type),
      sequence: event.sequence,
    }];
  });
  const userMoments = inputs.map((input) => ({
    id: `input-${input.id}`,
    kind: "user" as const,
    title: "用户提示",
    summary: input.content,
    timestamp: input.created_at,
    tone: "active" as const,
    sequence: input.revision,
    status: input.status,
  }));
  return [...systemMoments, ...userMoments].sort((left, right) => {
    const byTime = Date.parse(left.timestamp) - Date.parse(right.timestamp);
    return Number.isNaN(byTime) ? (left.sequence ?? 0) - (right.sequence ?? 0) : byTime;
  });
}

function usageFromEvents(events: RunEvent[], reportJson?: Record<string, unknown>): UsageState {
  const reportTokens = numberValue(reportJson?.total_tokens ?? reportJson?.token_count ?? reportJson?.tokens);
  const reportCost = numberValue(reportJson?.cost_usd ?? reportJson?.cost);
  const reportInput = numberValue(reportJson?.input_tokens);
  const reportOutput = numberValue(reportJson?.output_tokens);
  let tokens = reportTokens;
  let costUsd = reportCost;
  let inputTokens = reportInput;
  let outputTokens = reportOutput;
  for (const event of events) {
    if (!event.event_type.includes("cost") && !event.event_type.includes("usage")) continue;
    const { payload } = normalizeEvent(event);
    // ``cost.update`` carries cumulative totals.  Keep the latest event while
    // a run is live; the final report remains authoritative once available.
    const direct = numberValue(payload.total_tokens ?? payload.tokens);
    const input = numberValue(payload.input_tokens ?? payload.prompt_tokens) ?? 0;
    const output = numberValue(payload.output_tokens ?? payload.completion_tokens) ?? 0;
    if (reportTokens === undefined && (direct !== undefined || input || output)) {
      tokens = direct ?? input + output;
      inputTokens = input;
      outputTokens = output;
    }
    if (reportCost === undefined) {
      const eventCost = numberValue(payload.cost_usd ?? payload.usd ?? payload.cost);
      if (eventCost !== undefined) costUsd = eventCost;
    }
  }
  return { tokens, costUsd, inputTokens, outputTokens };
}

function hasUsage(value: { total_tokens?: number; input_tokens?: number; output_tokens?: number; cost_usd?: number; calls?: number } | undefined): boolean {
  return Boolean(value && (value.total_tokens || value.input_tokens || value.output_tokens || value.cost_usd || value.calls));
}

function formatTokens(value?: number): string {
  if (value === undefined) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

function formatCost(value?: number): string {
  return value === undefined ? "—" : `$${value.toFixed(4)}`;
}

function formatDuration(startedAt?: string | null, finishedAt?: string | null, now: number = Date.now()): string {
  if (!startedAt) return "—";
  const start = Date.parse(startedAt);
  const finish = finishedAt ? Date.parse(finishedAt) : now;
  if (Number.isNaN(start) || Number.isNaN(finish) || finish < start) return "—";
  const total = Math.floor((finish - start) / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return `${hours ? `${hours}:` : ""}${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

// A Worker emits one ``worker_command`` per shell tool call, capped upstream by
// ``WORKER_COMMAND_MAX_PER_WORKER``.  Keeping the whole tail would make the
// board as noisy as the raw log, but keeping none of it means the "thinking
// route" is unavailable after the fact -- so keep a bounded tail.
const WORKER_ROUTE_LIMIT = 8;

/**
 * Tick only the agent board while any worker is live.
 *
 * The elapsed clock has to advance even when the Run is silent -- a wall clock
 * frozen at "4:12" for six minutes is indistinguishable from a stalled agent,
 * which is exactly the question this board exists to answer.  The tick lives
 * inside the board so the case-board canvas and the event stream are not
 * re-rendered once a second.
 */
function useElapsedTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function silenceLabel(lastSeenAt: string | undefined, now: number): string {
  if (!lastSeenAt) return "";
  const seen = Date.parse(lastSeenAt);
  if (Number.isNaN(seen)) return "";
  const seconds = Math.max(0, Math.floor((now - seen) / 1000));
  if (seconds < 60) return `${seconds} 秒前有动作`;
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒前有动作`;
}

/**
 * Per-agent board: identity, live state, and the action trail.
 *
 * The old row said "role · engine · 活动中" and nothing else, so a Run that
 * burned 20 minutes produced a board that could not answer the only questions
 * an operator has -- what is each agent doing, what did it just run, did that
 * produce anything, and is it still alive.  Every number here comes from an
 * event the backend actually emits (``worker_step`` / ``worker_command``);
 * nothing is inferred.
 */
function AgentBoard({ workers }: { workers: WorkerState[] }) {
  const [openRoutes, setOpenRoutes] = useState<Record<string, boolean>>({});
  const now = useElapsedTick(workers.some((worker) => worker.status === "running"));
  if (!workers.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Worker" />;
  return (
    <div className="muteki-agent-board">
      {workers.map((worker) => {
        const running = worker.status === "running";
        const last = worker.commands[worker.commands.length - 1];
        const routeOpen = Boolean(openRoutes[worker.id]);
        return (
          <div className={`muteki-agent-card ${running ? "is-running" : "is-finished"}`} key={worker.id}>
            <div className="muteki-agent-head">
              <Badge status={running ? "processing" : "default"} />
              <strong>{worker.role}</strong>
              <Tag>{worker.engine}</Tag>
              <span className={`muteki-agent-clock ${running ? "is-live" : ""}`}>{formatDuration(worker.startedAt, worker.finishedAt, now)}</span>
            </div>
            <div className="muteki-agent-metrics">
              <span><b>{worker.steps}</b> 步</span>
              <span><b>{worker.toolSteps}</b> 工具</span>
              <span><b>{worker.commandCount}</b> 命令</span>
              <span><b>{worker.factCount}</b> fact</span>
              {worker.distinctTools.length > 0 && <span className="muteki-agent-tools">{worker.distinctTools.slice(0, 3).join(" / ")}</span>}
              {worker.resultCode && <span className="muteki-agent-code">{worker.resultCode}</span>}
            </div>
            {last ? (
              <>
                <div className="muteki-agent-command">
                  <span className="muteki-agent-prompt">$</span>
                  <ExpandableText value={last.command} lines={2} className="muteki-agent-command-text" />
                </div>
                {last.result && <ExpandableText value={last.result} lines={2} className="muteki-agent-result" />}
              </>
            ) : (
              <div className="muteki-agent-idle">{running ? "已启动 · 尚未发出命令（思考中）" : "本轮未记录任何命令"}</div>
            )}
            {worker.commands.length > 1 && (
              <button type="button" className="muteki-expand-toggle" onClick={() => setOpenRoutes((current) => ({ ...current, [worker.id]: !current[worker.id] }))}>
                {routeOpen ? "收起行动路线" : `行动路线 · ${worker.commands.length} 步`}
              </button>
            )}
            {routeOpen && (
              <ol className="muteki-agent-route">
                {[...worker.commands].reverse().map((record) => (
                  <li key={record.sequence}>
                    <code>{record.command}</code>
                    {record.result && <small>{record.result}</small>}
                  </li>
                ))}
              </ol>
            )}
            <div className="muteki-agent-foot">
              <span>{worker.intentId ? `intent ${worker.intentId}` : "无绑定 intent"}</span>
              {worker.branchId && <span>branch {worker.branchId}</span>}
              <span>{running ? silenceLabel(worker.lastSeenAt, now) : "已结束"}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function emptyWorker(id: string): WorkerState {
  return { id, role: "worker", engine: "—", status: "running", steps: 0, toolSteps: 0, distinctTools: [], commands: [], commandCount: 0, factCount: 0 };
}

/**
 * Running agents first, then most recent activity.
 *
 * Insertion order alone put a finished worker from 40 minutes ago above the one
 * that is working right now, which is the opposite of what the board is for.
 */
function orderWorkers(workers: Map<string, WorkerState>, factOwners: Map<string, number>): WorkerState[] {
  return [...workers.values()]
    .map((worker) => ({ ...worker, factCount: factOwners.get(worker.id) ?? 0 }))
    .sort((left, right) => {
      const running = Number(right.status === "running") - Number(left.status === "running");
      if (running !== 0) return running;
      return String(right.lastSeenAt ?? "").localeCompare(String(left.lastSeenAt ?? ""));
    });
}

// Exported for the projection harness: the Worker board is only correct if it
// survives a real Run's event stream, and that is checked off-line against
// events dumped from ``run_events`` rather than by eyeballing a screenshot.
export function projectBoard(events: RunEvent[], nativeState?: MutekiGraphState): BoardState {
  const workers = new Map<string, WorkerState>();
  const intents = new Map<string, IntentState>();
  const facts: FactState[] = [];
  // Facts are attributed by the emitting actor; recorded separately so a fact
  // that arrives before its worker_started still lands on the right agent.
  const factOwners = new Map<string, number>();
  let phase = "prepare";
  let flagFound = false;
  let reviewCount = 0;

  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    if (!event.event_type.startsWith("muteki.")) continue;
    const { type, actor, payload, verified } = normalizeEvent(event);
    if (type === "phase_changed") phase = text(payload.phase, phase).toLowerCase();
    // A historical Muteki flag event is not the same as the current Run's
    // terminal result. Keep only verified events in the board projection;
    // the formal Completion Gate/report remains the display authority for
    // the exact canonical flag.
    if (type === "flag_found" && verified === true) flagFound = true;
    if (type.startsWith("review_")) reviewCount += 1;

    if (type === "worker_started" || type === "worker_step" || type === "worker_command" || type === "worker_finished") {
      const workerId = text(payload.worker_id, actor);
      // Worker ids are reused within a Run: ``worker-1`` came back three times
      // in one measured Run (04:59 known-path, 05:01 route:9d21, 05:10
      // route:4922) and 73 of 3,864 ids restart at least once.  Those are
      // separate shifts -- different branch, different counters -- so the card
      // must show the current one, not a lifetime average that no agent ever
      // lived.  A repeat start on the *same* branch is a retry: also a reset.
      const existing = workers.get(workerId);
      const incomingBranch = text(payload.branch_id);
      const restart = type === "worker_started" && existing !== undefined
        && (existing.status === "finished" || Boolean(incomingBranch && existing.branchId && incomingBranch !== existing.branchId));
      const previous = (restart ? undefined : existing) ?? emptyWorker(workerId);
      const next: WorkerState = { ...previous, lastSeenAt: event.created_at };
      next.role = text(payload.role, previous.role);
      next.engine = text(payload.engine_id, previous.engine);
      next.intentId = text(payload.intent_id, previous.intentId);
      next.branchId = text(payload.branch_id, previous.branchId);
      if (type === "worker_started") {
        next.status = "running";
        next.startedAt = event.created_at;
        next.finishedAt = undefined;
        next.resultCode = undefined;
      }
      if (type === "worker_step") {
        // Snapshots carry cumulative totals; the aggregate is coalesced, so a
        // delta would undercount when several steps land inside one window.
        const total = Number(payload.steps_total);
        if (Number.isFinite(total) && total > 0) next.steps = Math.max(previous.steps, total);
        const toolSteps = Number(payload.tool_steps);
        if (Number.isFinite(toolSteps) && toolSteps >= 0) next.toolSteps = Math.max(previous.toolSteps, toolSteps);
        if (Array.isArray(payload.distinct_tools)) {
          next.distinctTools = payload.distinct_tools.filter((item): item is string => typeof item === "string" && Boolean(item));
        }
        // A returning worker's trailing aggregate is published as a
        // ``worker_step`` whose kind is ``worker_finished``; for a coalesced
        // worker that is the only close signal it ever sends.
        if (text(payload.kind) === "worker_finished") {
          next.status = "finished";
          next.finishedAt = event.created_at;
          next.resultCode = text(payload.worker_result_code, previous.resultCode);
        }
      }
      if (type === "worker_command") {
        const command = text(payload.command);
        const result = text(payload.result_excerpt);
        const kind = text(payload.kind);
        const tail = previous.commands[previous.commands.length - 1];
        // One shell call is published twice: ``kind="tool"`` with the command
        // and an empty excerpt, then ``kind="tool_result"`` with the same
        // command plus the output.  Measured on a real Run: 89 and 90 events
        // for 44 and 45 actual calls.  Folding the pair is what keeps
        // "命令 N" equal to the number of commands actually run -- and a
        // stray tool_result must never be counted as a new command.
        // The result event also has to be recognised when its excerpt is
        // empty: keying the fold on "has output" under-counted the fold and
        // left a duplicate record for every silent tool call.
        const closesTail = kind === "tool_result"
          || (!command && Boolean(result))
          || (!kind && Boolean(result) && Boolean(tail) && !tail?.result);
        if (closesTail) {
          if (tail && !tail.result) next.commands = [...previous.commands.slice(0, -1), { ...tail, result }];
        } else if (command) {
          next.commands = [...previous.commands, { sequence: event.sequence, command, result, at: event.created_at }].slice(-WORKER_ROUTE_LIMIT);
          next.commandCount = previous.commandCount + 1;
        }
      }
      if (type === "worker_finished") {
        next.status = "finished";
        next.finishedAt = event.created_at;
        next.resultCode = text(payload.worker_result_code, previous.resultCode);
      }
      workers.set(workerId, next);
    }
    if (type === "intent_proposed") {
      const intentId = text(payload.intent_id, `intent-${event.sequence}`);
      const intentPayload = isRecord(payload.payload) ? payload.payload : {};
      intents.set(intentId, {
        id: intentId,
        description: text(payload.description, "未命名 Intent"),
        status: "open",
        tool: text(intentPayload.tool_name),
        classification: text(intentPayload.classification),
      });
    }
    if (type === "intent_claimed" || type === "intent_concluded" || type === "intent_released") {
      const intentId = text(payload.intent_id);
      const previous = intents.get(intentId);
      if (!previous) continue;
      intents.set(intentId, {
        ...previous,
        status: type === "intent_concluded" ? "done" : type === "intent_claimed" ? "claimed" : "open",
        worker: type === "intent_released" ? undefined : type === "intent_claimed" ? actor : previous.worker,
        result: text(payload.result, previous.result),
      });
    }
    if (type === "fact_added") {
      const refs = Array.isArray(payload.evidence_refs) ? payload.evidence_refs : [];
      facts.push({ sequence: event.sequence, content: text(payload.content, "未命名事实"), verified: Boolean(payload.verified ?? verified), evidenceCount: refs.length, state: "unresolved" });
      factOwners.set(actor, (factOwners.get(actor) ?? 0) + 1);
    }
    // Fact lifecycle. A rejected/merged/superseded fact is a conclusion: it
    // must leave the pending list instead of sitting there as "待验证"
    // forever, which is what made the board look like an ever-growing pile of
    // open questions. Challenged/revalidated keep the fact visible but state
    // its review status.
    if (type === "fact_rejected" || type === "fact_merged" || type === "fact_superseded") {
      const target = Number(payload.fact_seq ?? 0);
      const index = facts.findIndex((fact) => fact.sequence === target);
      if (index >= 0) facts.splice(index, 1);
    }
    if (type === "fact_challenged" || type === "fact_revalidated") {
      const target = Number(payload.fact_seq ?? 0);
      const fact = facts.find((item) => item.sequence === target);
      if (fact) fact.state = type === "fact_challenged" ? "challenged" : "revalidated";
    }
  }

  // The official SharedGraph is the Muteki Blackboard authority.  MySQL SSE
  // events remain useful for live Worker/audit detail, but they are bounded
  // and can arrive after a reconnect.  Prefer the read-only native snapshot
  // for facts/intents/flags whenever it is available so the Workspace cannot
  // display an empty Blackboard while the run-scoped graph is populated.
  if (nativeState?.available) {
    // The graph is authoritative for fact ownership too: the SSE window is
    // bounded, so an event-only count under-reports a long round.
    const nativeOwners = new Map<string, number>();
    for (const fact of nativeState.facts) {
      const owner = text(fact.source_worker_id);
      if (owner) nativeOwners.set(owner, (nativeOwners.get(owner) ?? 0) + 1);
    }
    return {
      phase,
      flagFound: flagFound || nativeState.flags.some((item) => item.verified),
      workers: orderWorkers(workers, nativeOwners),
      intents: [...nativeState.intents].sort((left, right) => (right.sequence ?? 0) - (left.sequence ?? 0)).map((intent) => ({
        id: intent.id,
        description: intent.description || "未命名 Intent",
        status: intent.status,
        worker: intent.worker ?? undefined,
        result: intent.result,
      })),
      // Verified facts first: the confirmed picture is what the operator is
      // looking for, and putting the newest pending candidate on top is what
      // buried it.
      facts: [...nativeState.facts]
        .sort((left, right) => Number(right.verified) - Number(left.verified) || right.sequence - left.sequence)
        .map((fact) => ({
          sequence: fact.sequence,
          content: fact.content || "未命名事实",
          verified: fact.verified,
          evidenceCount: fact.evidence_refs.length,
          state: fact.state === "challenged" || fact.state === "revalidated" ? fact.state : "unresolved",
        })),
      reviewCount,
    };
  }
  return { phase, flagFound, workers: orderWorkers(workers, factOwners), intents: [...intents.values()].reverse(), facts: [...facts].sort((left, right) => Number(right.verified) - Number(left.verified) || right.sequence - left.sequence), reviewCount };
}

type CaseBoardFact = {
  sequence: number;
  content: string;
  summary_zh?: string;
  verified: boolean;
  evidence_refs: string[];
  source_worker_id?: string;
};

function projectCaseBoard(nativeState: MutekiGraphState | undefined, fallback: BoardState, canonicalFlag?: string): CaseBoardSections {
  const confirmed: CaseBoardCard[] = [];
  const pending: CaseBoardCard[] = [];
  const semanticByCard = new Map((nativeState?.board_semantic?.items ?? []).map((item) => [item.card_id, item]));

  // The native SharedGraph is the authority: ``key_conditions`` holds the facts
  // the backend already gated as confirmed, while ``facts`` still carries every
  // non-retired fact — including the unverified ones that used to be filtered
  // out of the board entirely.
  const nativeConfirmed: CaseBoardFact[] = (nativeState?.available ? nativeState.key_conditions ?? [] : []).map((fact) => ({
    sequence: fact.sequence,
    content: fact.content,
    summary_zh: fact.summary_zh,
    verified: fact.verified,
    evidence_refs: fact.evidence_refs ?? [],
    source_worker_id: fact.source_worker_id,
  }));
  const fallbackFacts: CaseBoardFact[] = fallback.facts.map((fact) => ({
    sequence: fact.sequence,
    content: fact.content,
    verified: fact.verified,
    evidence_refs: Array.from({ length: fact.evidenceCount }, (_, index) => `evidence-${index + 1}`),
  }));
  const isConfirmedFact = (fact: CaseBoardFact) => fact.verified && fact.evidence_refs.length > 0;
  const confirmedFacts = nativeState?.available ? nativeConfirmed : fallbackFacts.filter(isConfirmedFact);
  const confirmedSequences = new Set(confirmedFacts.map((fact) => fact.sequence));
  // Confirmed vs. pending is a *split*, never a drop: everything that is not a
  // confirmed key condition is still rendered, just in its own section.
  const pendingFacts: CaseBoardFact[] = nativeState?.available
    ? (nativeState.facts ?? [])
      .filter((fact) => !confirmedSequences.has(fact.sequence))
      .map((fact) => ({
        sequence: fact.sequence,
        content: fact.content,
        verified: fact.verified,
        evidence_refs: fact.evidence_refs ?? [],
        source_worker_id: fact.source_worker_id,
      }))
    : fallbackFacts.filter((fact) => !isConfirmedFact(fact));

  const buildFactCard = (fact: CaseBoardFact): CaseBoardCard | undefined => {
    const content = text(fact.content).trim();
    if (!content) return undefined;
    const semantic = semanticByCard.get(`fact-${fact.sequence}`)?.summary_zh?.trim();
    const detail = fact.summary_zh?.trim();
    // The body is always the original fact text. The Codex/board summary is
    // demoted to a subtitle, so a shorter summary can no longer stand in for
    // (and therefore hide) the evidence text.
    const summary = [semantic, detail].find((item) => item && item !== content && !content.startsWith(item));
    return {
      id: `fact-${fact.sequence}-${content}`,
      kind: /flag|password|credential|账号|密码|token/i.test(content) ? "finding" : "fact",
      content,
      summary,
      sourceContent: content,
      verified: fact.verified,
      evidenceRefs: fact.evidence_refs ?? [],
      source: fact.source_worker_id,
      sequence: fact.sequence,
    };
  };

  for (const fact of confirmedFacts) {
    const card = buildFactCard(fact);
    if (card) confirmed.push(card);
  }
  for (const fact of pendingFacts) {
    const card = buildFactCard(fact);
    if (card) pending.push(card);
  }
  for (const flag of nativeState?.flags ?? []) {
    if (!flag.flag || !flag.verified || !flag.evidence_refs.length) continue;
    confirmed.push({
      id: `flag-${flag.sequence}-${flag.flag}`,
      kind: "flag",
      content: flag.flag,
      verified: flag.verified,
      evidenceRefs: flag.evidence_refs ?? [],
      source: "Completion Gate",
      sequence: flag.sequence,
    });
  }
  for (const poc of nativeState?.pocs ?? []) {
    const label = poc.name || poc.poc_id;
    if (!label || poc.status === "rejected" || !poc.artifact_id) continue;
    confirmed.push({
      id: `poc-${poc.poc_id}`,
      kind: "finding",
      content: `PoC: ${label}${poc.note ? ` — ${poc.note}` : ""}`,
      verified: poc.status !== "rejected",
      evidenceRefs: poc.artifact_id ? [poc.artifact_id] : [],
      source: "Muteki PoC",
      sequence: confirmed.length + 1,
    });
  }
  if (canonicalFlag && !confirmed.some((card) => card.kind === "flag" && card.content === canonicalFlag)) {
    confirmed.push({ id: "canonical-flag", kind: "flag", content: canonicalFlag, verified: true, evidenceRefs: [], source: "Completion Gate", sequence: confirmed.length + 1 });
  }
  return {
    confirmed: confirmed.sort((left, right) => left.sequence - right.sequence),
    pending: pending.sort((left, right) => left.sequence - right.sequence),
  };
}

function CaseNote({ card, section, expanded, fullscreen, style, onToggleExpand, onPointerDown }: {
  card: CaseBoardCard;
  section: "confirmed" | "pending";
  expanded: boolean;
  fullscreen: boolean;
  style: CSSProperties;
  onToggleExpand: (cardId: string) => void;
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [clamped, setClamped] = useState(false);

  // Measure the *unclamped* height so the expand control only shows up when the
  // text actually exceeds the collapsed 4-line box. Without this every short
  // note would carry a pointless "展开" button.
  useEffect(() => {
    const body = bodyRef.current;
    if (!body || expanded) return;
    const frame = window.requestAnimationFrame(() => {
      const collapsedHeight = body.clientHeight;
      const display = body.style.display;
      body.style.display = "block";
      body.style.setProperty("-webkit-line-clamp", "unset");
      const fullHeight = body.scrollHeight;
      body.style.display = display;
      body.style.removeProperty("-webkit-line-clamp");
      setClamped(fullHeight > collapsedHeight + 1);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [card.content, expanded, fullscreen]);

  const head = section === "pending"
    ? "未验证线索"
    : card.kind === "flag" ? "最终答案" : card.kind === "finding" ? "关键发现" : "已确认事实";
  const provenance = section === "pending"
    ? card.verified ? "已验证但未列入关键条件" : "未验证 · 仅供推断参考"
    : "已验证";

  return (
    <article
      className={`muteki-case-note is-${card.kind} ${section === "pending" ? "is-pending" : "is-verified"} ${expanded ? "is-expanded" : ""}`}
      data-board-card-id={card.id}
      style={style}
      onPointerDown={onPointerDown}
    >
      <div className="muteki-case-note-head"><strong>{head}</strong><span>#{card.sequence}</span></div>
      <div ref={bodyRef} className={`muteki-case-note-content ${expanded ? "is-expanded" : ""}`}>{card.content}</div>
      {card.summary ? <div className="muteki-case-note-summary">{card.summary}</div> : null}
      {(clamped || expanded) && (
        <button
          type="button"
          className="muteki-case-note-toggle"
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => { event.stopPropagation(); onToggleExpand(card.id); }}
        >
          {expanded ? "收起" : `展开全文 · ${card.content.trim().length} 字`}
        </button>
      )}
      <small>{provenance} · 证据 {card.evidenceRefs.length} 条{card.source ? ` · 来源 ${card.source}` : ""}</small>
    </article>
  );
}

function CaseBoardCanvas({ cards, pendingCards, fullscreen, positions, semantic, currentRevision, expandedCards, onToggleExpand, onPositionChange, onArrange, onAnalyze, onToggleFullscreen }: {
  cards: CaseBoardCard[];
  pendingCards: CaseBoardCard[];
  fullscreen: boolean;
  positions: Record<string, BoardPosition>;
  semantic?: MutekiBoardSemantic;
  currentRevision?: number;
  expandedCards: Record<string, boolean>;
  onToggleExpand: (cardId: string) => void;
  onPositionChange: (cardId: string, position: BoardPosition) => void;
  onArrange: () => void;
  onAnalyze: () => void;
  onToggleFullscreen: () => void;
}) {
  const semanticCurrent = semantic?.status === "completed" && semantic.revision === currentRevision;
  const dragRef = useRef<{ cardId: string; offsetX: number; offsetY: number; width: number; height: number }>();

  const beginDrag = (event: ReactPointerEvent<HTMLElement>, cardId: string) => {
    // Dragging must not steal pointer events from expanded text selection or
    // from the expand/collapse button.
    const target = event.target as HTMLElement | null;
    if (target?.closest(".muteki-case-note-content.is-expanded, .muteki-case-note-toggle")) return;
    const card = event.currentTarget.getBoundingClientRect();
    dragRef.current = {
      cardId,
      offsetX: event.clientX - card.left,
      offsetY: event.clientY - card.top,
      width: card.width,
      height: card.height,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    // Resolve the board from the element handling the event so both the
    // confirmed and the pending section can host draggable notes.
    const board = event.currentTarget;
    const bounds = board.getBoundingClientRect();
    const maxX = Math.max(0, bounds.width - drag.width - 8);
    const maxY = Math.max(0, bounds.height - drag.height - 8);
    const x = Math.max(0, Math.min(maxX, event.clientX - bounds.left + board.scrollLeft - drag.offsetX));
    const y = Math.max(0, Math.min(maxY, event.clientY - bounds.top + board.scrollTop - drag.offsetY));
    onPositionChange(drag.cardId, { x: (x / Math.max(1, bounds.width)) * 100, y: (y / Math.max(1, bounds.height)) * 100 });
  };

  const endDrag = () => { dragRef.current = undefined; };
  const boardHandlers = { onPointerMove: moveDrag, onPointerUp: endDrag, onPointerCancel: endDrag };
  const renderNote = (card: CaseBoardCard, index: number, section: "confirmed" | "pending") => {
    const position = positions[card.id] ?? defaultBoardPosition(index);
    return (
      <CaseNote
        key={card.id}
        card={card}
        section={section}
        expanded={Boolean(expandedCards[card.id])}
        fullscreen={fullscreen}
        style={{ left: `${position.x}%`, top: `${position.y}%` }}
        onToggleExpand={onToggleExpand}
        onPointerDown={(event) => beginDrag(event, card.id)}
      />
    );
  };
  // A note grows past the board's fixed height when expanded; switching the
  // board to a scroll container keeps the whole text reachable instead of
  // clipping it again at the board edge.
  const hasExpandedNote = [...cards, ...pendingCards].some((card) => expandedCards[card.id]);

  return (
    <>
      <div className="muteki-case-board-toolbar">
        <div className="muteki-case-board-legend">
          <span><i className="is-fact" /> 已确认事实</span>
          <span><i className="is-finding" /> 关键发现</span>
          <span><i className="is-verified" /> 有证据支撑</span>
          {pendingCards.length ? <span><i className="is-pending" /> 未验证 {pendingCards.length}</span> : null}
        </div>
        <Space size={4}>
          <Button size="small" type="text" onClick={onArrange}>自动整理</Button>
          <Button size="small" type="text" loading={semantic?.status === "running"} disabled={!cards.length || semantic?.status === "running" || semanticCurrent} onClick={onAnalyze}>
            {semanticCurrent ? "AI 已解析" : semantic?.status === "completed" ? "重新解析" : semantic?.status === "failed" ? "重试 AI 解析" : "AI 解析"}
          </Button>
          <Button size="small" type="text" icon={fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />} onClick={onToggleFullscreen}>
            {fullscreen ? "退出全屏" : "全屏查看"}
          </Button>
        </Space>
      </div>
      <div className={`muteki-case-board ${fullscreen ? "is-fullscreen-board" : ""} ${hasExpandedNote ? "has-expanded-note" : ""}`} {...boardHandlers}>
        {cards.length ? cards.map((card, index) => renderNote(card, index, "confirmed")) : <div className="muteki-case-board-empty"><PushpinOutlined /><span>等待 Blackboard 写入已确认条件</span><small>已验证且带证据引用的关键线索显示在这里；未验证线索单独放在下方分区</small></div>}
      </div>
      {pendingCards.length ? (
        <section className="muteki-case-board-pending">
          <div className="muteki-case-board-pending-head">
            <span><ClockCircleOutlined /> 未验证线索</span>
            <small>{pendingCards.length} 条 · 尚未通过证据校验，仅供参考</small>
          </div>
          <div className={`muteki-case-board is-pending-board ${fullscreen ? "is-fullscreen-board" : ""} ${hasExpandedNote ? "has-expanded-note" : ""}`} {...boardHandlers}>
            {pendingCards.map((card, index) => renderNote(card, index, "pending"))}
          </div>
        </section>
      ) : null}
    </>
  );
}

function statusColor(status: IntentState["status"]): string {
  return status === "done" ? "green" : status === "claimed" ? "blue" : "gold";
}

function statusLabel(status: IntentState["status"]): string {
  return status === "done" ? "完成" : status === "claimed" ? "执行中" : "待认领";
}

function flagMeta(state: FlagCandidate["review_state"]) {
  if (state === "VALID") return { color: "green", label: "已验证" };
  if (state === "INVALID") return { color: "red", label: "已否决" };
  return { color: "gold", label: "待复核" };
}

// Confidence is about how close the evidence is to a flag, not how sure the
// model feels, so the wording says what to do with it rather than "90%".
function directionLabel(confidence: "high" | "medium" | "low") {
  if (confidence === "high") return "优先做";
  if (confidence === "medium") return "值得一试";
  return "证据不足";
}

function StageRail({ phase, terminal }: { phase: string; terminal: boolean }) {
  const active = Math.max(0, stages.findIndex((stage) => stage.key === phase));
  return (
    <div className="muteki-compact-rail">
      {stages.map((stage, index) => {
        const done = index < active || (terminal && index <= active);
        const current = index === active && !terminal;
        return (
          <div className={`muteki-compact-stage ${done ? "is-done" : ""} ${current ? "is-current" : ""}`} key={stage.key}>
            <span className="muteki-compact-node">{done ? <CheckCircleFilled /> : index + 1}</span>
            <span><strong>{stage.label}</strong><small>{stage.note}</small></span>
          </div>
        );
      })}
    </div>
  );
}

export function WorkspacePage() {
  const { id = "" } = useParams();
  const client = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [continuation, setContinuation] = useState("");
  const [artifactContent, setArtifactContent] = useState<{ path: string; content: string }>();
  const [pocExporting, setPocExporting] = useState(false);
  const [writeupExporting, setWriteupExporting] = useState(false);
  const [caseBoardFullscreen, setCaseBoardFullscreen] = useState(false);
  const [caseBoardLayout, setCaseBoardLayout] = useState<BoardLayout>(() => readBoardLayout(id));
  // Expansion is keyed by card id so it survives the 2s polling re-render and
  // is shared between the inline board and the fullscreen board.
  const [expandedCaseCards, setExpandedCaseCards] = useState<Record<string, boolean>>({});
  // The inspector only renders the first 8 facts by default; the toggle opens
  // the rest instead of silently hiding them.
  const [showAllFacts, setShowAllFacts] = useState(false);

  useEffect(() => {
    setCaseBoardLayout(readBoardLayout(id));
    setExpandedCaseCards({});
    setShowAllFacts(false);
  }, [id]);

  useEffect(() => {
    if (!id || caseBoardLayout.runId !== id || typeof window === "undefined") return;
    window.localStorage.setItem(`muteki-board-layout:${id}`, JSON.stringify(caseBoardLayout));
  }, [caseBoardLayout, id]);

  const run = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.getRun(id),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && terminalStatuses.includes(status) ? false : 2000;
    },
  });
  const terminal = Boolean(run.data && isTerminalRunStatus(run.data.status));
  const liveRefetchInterval = terminal ? false : 2500;
  const solverState = useQuery({ queryKey: ["solver-state", id], queryFn: () => api.getSolverState(id), refetchInterval: liveRefetchInterval });
  const health = useQuery({ queryKey: ["run-health", id], queryFn: () => api.getRunHealth(id), refetchInterval: liveRefetchInterval });
  const runMessages = useQuery({ queryKey: ["run-messages", id], queryFn: () => api.getRunMessages(id), refetchInterval: liveRefetchInterval });
  const mutekiState = useQuery({
    queryKey: ["muteki-state", id],
    queryFn: () => api.getMutekiState(id),
    enabled: run.data?.solver_mode === "muteki",
    refetchInterval: (query) => (query.state.data?.board_semantic?.status === "running" ? 1500 : liveRefetchInterval),
  });
  const boardSemantic = mutekiState.data?.board_semantic;
  const usageSummary = useQuery({ queryKey: ["run-usage", id], queryFn: () => api.getRunUsage(id), refetchInterval: liveRefetchInterval });
  const tools = useQuery({ queryKey: ["tool-calls", id], queryFn: () => api.getToolCalls(id), refetchInterval: liveRefetchInterval });
  const observations = useQuery({ queryKey: ["observations", id], queryFn: () => api.getObservations(id), refetchInterval: liveRefetchInterval });
  const artifacts = useQuery({ queryKey: ["artifacts", id], queryFn: () => api.getArtifacts(id), refetchInterval: liveRefetchInterval });
  const flags = useQuery({ queryKey: ["flags", id], queryFn: () => api.getFlags(id), refetchInterval: liveRefetchInterval });
  const report = useQuery({
    queryKey: ["report", id],
    queryFn: () => api.getReport(id),
    retry: false,
    refetchInterval: boardSemantic?.status === "running" ? 1500 : false,
  });
  // Diagnostics scan every event, tool output and observation of the Run, so
  // this is deliberately *not* polled: it is fetched once the Run is over,
  // which is the only moment a "what should we do next" answer is meaningful.
  const diagnostics = useQuery({
    queryKey: ["run-diagnostics", id],
    queryFn: () => api.getRunDiagnostics(id),
    enabled: terminal,
    retry: false,
    staleTime: 60_000,
  });
  const board = useMemo(() => projectBoard(events, mutekiState.data), [events, mutekiState.data]);
  const keyMoments = useMemo(() => projectKeyMoments(events, runMessages.data ?? []), [events, runMessages.data]);
  const eventUsage = useMemo(() => usageFromEvents(events, report.data?.report_json), [events, report.data?.report_json]);
  const usage = useMemo(() => {
    const total = usageSummary.data?.total;
    if (!total) return eventUsage;
    if (!hasUsage(total)) {
      // A successful usage response with all-zero counters is still a real
      // state for a newly-started Run. Keep the dashboard numeric instead of
      // turning the authoritative zero into an undefined em dash. If the SSE
      // stream already has a non-zero cost event, prefer that live evidence.
      if (hasUsage(eventUsage)) return eventUsage;
      return { costUsd: 0, tokens: 0, inputTokens: 0, outputTokens: 0, calls: 0 };
    }
    return {
      costUsd: total.cost_usd,
      tokens: total.total_tokens,
      inputTokens: total.input_tokens,
      outputTokens: total.output_tokens,
      calls: total.calls,
    };
  }, [eventUsage, usageSummary.data?.total]);
  const modelUsages = usageSummary.data?.models ?? [];
  const canonicalFlag = useMemo(
    () => verifiedCanonicalFlag(run.data?.status, report.data?.report_json, report.data?.flag_pattern),
    [run.data?.status, report.data?.report_json, report.data?.flag_pattern],
  );
  const caseBoard = useMemo(() => projectCaseBoard(mutekiState.data, board, canonicalFlag), [board, canonicalFlag, mutekiState.data]);
  const semanticAnalysis = useMutation({
    mutationFn: () => api.startMutekiBoardSemanticAnalysis(id),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["muteki-state", id] });
      void client.invalidateQueries({ queryKey: ["report", id] });
      if (result.status === "running") message.success("Codex 语义解析已在后台开始，解题流程不会中断");
      else if (result.status === "completed") message.success("分析板语义解析已完成");
    },
    onError: (error: Error) => message.error(error.message),
  });
  useEffect(() => {
    if (boardSemantic?.status === "completed" || boardSemantic?.status === "failed") {
      void client.invalidateQueries({ queryKey: ["report", id] });
    }
  }, [boardSemantic?.status, client, id]);
  const updateCaseBoardPosition = (cardId: string, position: BoardPosition) => {
    setCaseBoardLayout((current) => ({ runId: id, positions: { ...current.positions, [cardId]: position } }));
  };
  const toggleCaseCardExpanded = (cardId: string) => {
    setExpandedCaseCards((current) => ({ ...current, [cardId]: !current[cardId] }));
  };
  const arrangeCaseBoard = () => {
    const positions: Record<string, BoardPosition> = {};
    caseBoard.confirmed.forEach((card, index) => { positions[card.id] = defaultBoardPosition(index); });
    caseBoard.pending.forEach((card, index) => { positions[card.id] = defaultBoardPosition(index); });
    setCaseBoardLayout({ runId: id, positions });
  };
  const pendingVerifiedFlag = board.flagFound && !canonicalFlag;
  const currentPhase = board.phase || run.data?.current_phase?.toLowerCase() || "prepare";

  const start = useMutation({
    mutationFn: () => api.startRun(id),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["run", id] }); message.success("任务已启动"); },
    onError: (error: Error) => message.error(error.message),
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelRun(id),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["run", id] }); message.success("任务已取消"); },
    onError: (error: Error) => message.error(error.message),
  });
  const restart = useMutation({
    mutationFn: () => api.restartRun(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["run", id] });
      void client.invalidateQueries({ queryKey: ["solver-state", id] });
      void client.invalidateQueries({ queryKey: ["runs"] });
      message.success("任务已重启，将沿用已有状态与证据继续执行");
    },
    onError: (error: Error) => message.error(error.message),
  });
  const reviewFlag = useMutation({
    mutationFn: (payload: { candidateId: string; reviewState: "OPEN" | "VALID" | "INVALID" }) => api.reviewFlagCandidate(id, payload.candidateId, payload.reviewState),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["flags", id] });
      void client.invalidateQueries({ queryKey: ["run", id] });
      void client.invalidateQueries({ queryKey: ["report", id] });
      message.success("Flag 状态已更新");
    },
    onError: (error: Error) => message.error(error.message),
  });
  const exportPoc = async () => {
    if (!canonicalFlag || pocExporting) return;
    setPocExporting(true);
    try {
      const result = await api.downloadMutekiPocBundle(id);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      message.success("可复现 PoC 包已导出");
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setPocExporting(false);
    }
  };

  const exportWriteup = async () => {
    if (writeupExporting) return;
    setWriteupExporting(true);
    try {
      const result = await api.downloadRunReport(id);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      message.success("Writeup 已导出");
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setWriteupExporting(false);
    }
  };

  useEffect(() => {
    setEvents([]);
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const pending = new Set<string>();
    const source = api.streamRunEvents(id, (event) => {
      setEvents((current) => current.some((item) => item.sequence === event.sequence) ? current : [...current, event].sort((a, b) => a.sequence - b.sequence).slice(-500));
      pending.add(event.event_type);
      if (refreshTimer !== undefined) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = undefined;
        const types = [...pending];
        pending.clear();
        void client.invalidateQueries({ queryKey: ["run", id] });
        if (types.some((type) => type.startsWith("muteki.") || type.startsWith("solver."))) {
          void client.invalidateQueries({ queryKey: ["solver-state", id] });
          void client.invalidateQueries({ queryKey: ["muteki-state", id] });
          void client.invalidateQueries({ queryKey: ["run-health", id] });
          void client.invalidateQueries({ queryKey: ["tools", id] });
          void client.invalidateQueries({ queryKey: ["observations", id] });
        }
        if (types.some((type) => type === "user_input.received" || type === "user_input.consumed" || type === "user.input_consumed")) {
          void client.invalidateQueries({ queryKey: ["run-messages", id] });
        }
        if (types.some((type) => type.startsWith("tool."))) {
          void client.invalidateQueries({ queryKey: ["tool-calls", id] });
          void client.invalidateQueries({ queryKey: ["observations", id] });
        }
        if (types.some((type) => type.startsWith("artifact."))) void client.invalidateQueries({ queryKey: ["artifacts", id] });
        if (types.some((type) => type === "run.completed" || type.startsWith("flag.") || type === "muteki.flag_found")) {
          void client.invalidateQueries({ queryKey: ["flags", id] });
          void client.invalidateQueries({ queryKey: ["report", id] });
        }
        // A Muteki Run can go terminal through the native handoff rather than
        // the in-process runtime.  Its report/Writeup and PoC only become
        // exportable after the terminal event, so refresh them here too.
        if (types.some((type) => type === "muteki.run_finished" || type === "run.muteki_terminal_reconciled")) {
          void client.invalidateQueries({ queryKey: ["report", id] });
          void client.invalidateQueries({ queryKey: ["flags", id] });
          void client.invalidateQueries({ queryKey: ["artifacts", id] });
        }
      }, 220);
    });
    return () => {
      source.close();
      if (refreshTimer !== undefined) clearTimeout(refreshTimer);
    };
  }, [client, id]);

  useEffect(() => {
    if (!caseBoardFullscreen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setCaseBoardFullscreen(false);
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [caseBoardFullscreen]);

  const flagRows = flags.data ?? [];
  const outcome = diagnostics.data?.outcome ?? null;
  const direction = diagnostics.data?.next_direction ?? null;
  const verifiedCount = Math.max(board.facts.filter((fact) => fact.verified).length, flagRows.filter((item) => item.review_state === "VALID").length);
  const candidateCount = Math.max(events.filter((event) => event.event_type === "muteki.flag_candidate" || event.event_type === "flag.candidate_found").length, flagRows.filter((item) => item.review_state === "OPEN").length);
  const deadEndCount = events.filter((event) => event.event_type === "muteki.dead_end").length;
  const activeWorkerCount = board.workers.filter((worker) => worker.status === "running").length;
  const workerTotal = board.workers.length;

  return (
    <div className="muteki-workspace">
      <header className="muteki-run-header">
        <div className="muteki-run-title">
          <div className="muteki-kicker"><span className="muteki-live-dot" /> MUTEKI / SOLVER LOOP</div>
          <div className="muteki-title-line">
            <h1>{run.data?.title ?? run.data?.challenge_name ?? run.data?.challenge_id ?? "解题工作区"}</h1>
            <Tag>{solverModeLabel(run.data?.solver_mode)}</Tag>
            <Tag color="cyan">{currentPhase.toUpperCase()}</Tag>
          </div>
          <span className="muteki-run-id">{run.data?.id ?? id}</span>
        </div>
        <Space wrap>
          {run.data && <RunStatusTag status={run.data.status} />}
          <Button type="primary" icon={<CaretRightOutlined />} loading={start.isPending} disabled={!run.data || !startableRunStatuses.includes(run.data.status)} onClick={() => start.mutate()}>
            {run.data?.status === "CREATED" ? "启动" : "恢复"}
          </Button>
          <Button icon={<ReloadOutlined />} loading={restart.isPending} disabled={!run.data || !restartableRunStatuses.includes(run.data.status)} onClick={() => restart.mutate()}>重启</Button>
          <Button danger icon={<PauseCircleOutlined />} loading={cancel.isPending} disabled={!run.data || terminalStatuses.includes(run.data.status)} onClick={() => cancel.mutate()}>停止</Button>
        </Space>
      </header>

      <div className="muteki-status-strip">
        <div className="muteki-status-main"><Badge status={terminal ? "success" : "processing"} /><strong>{terminal ? "RUN CLOSED" : "RUNNING"}</strong><span>{terminal ? "Solver 已停止接受新 Intent" : "Blackboard 正在驱动下一轮循环"}</span></div>
        <div className="muteki-stat"><span>已验证</span><strong className="is-green">{verifiedCount}</strong></div>
        <div className="muteki-stat"><span>候选</span><strong className="is-gold">{candidateCount}</strong></div>
        <div className="muteki-stat"><span>意图</span><strong className="is-blue">{board.intents.length}</strong></div>
        <div className="muteki-stat"><span>死路</span><strong className="is-danger">{deadEndCount}</strong></div>
        <div className="muteki-stat"><span>WORKER</span><strong>{activeWorkerCount}/{workerTotal}</strong></div>
        <div className="muteki-stat"><span>成本</span><strong>{formatCost(usage.costUsd)}</strong></div>
        <div className="muteki-stat"><span>TOKEN</span><strong>{formatTokens(usage.tokens)}</strong></div>
        <div className="muteki-stat"><span>总耗时</span><strong>{formatDuration(run.data?.started_at, run.data?.finished_at)}</strong></div>
      </div>

      <details className="muteki-usage-details">
        <summary><span>模型用量明细</span><span>{modelUsages.length ? `${modelUsages.length} 个模型 · 展开查看` : "暂无已记录用量"}</span></summary>
        {modelUsages.length ? <div className="muteki-usage-table">
          <div className="muteki-usage-row muteki-usage-row-head"><span>模型 / 角色</span><span>调用</span><span>输入</span><span>输出</span><span>Token</span><span>成本</span></div>
          {modelUsages.map((item) => <div className="muteki-usage-row" key={`${item.model}-${item.role}-${item.source}`}><span><strong>{item.model}</strong><small>{item.role}</small></span><span>{item.calls}</span><span>{formatTokens(item.input_tokens)}</span><span>{formatTokens(item.output_tokens)}</span><span>{formatTokens(item.total_tokens)}</span><span>{formatCost(item.cost_usd)}</span></div>)}
        </div> : <div className="muteki-usage-empty">当前运行尚未收到引擎用量回报；不会用估算值冒充真实 Token。</div>}
      </details>

      <Alert className="muteki-safety-bar" showIcon icon={<SafetyCertificateOutlined />} type="info" message="授权边界已启用：Worker 只能通过当前 Run 的 Tool Gateway 访问允许主机与工作区。" />

      <div className="muteki-main-layout">
        <main className="muteki-main-column">
          <Card className="muteki-loop-card" title={<Space><ThunderboltOutlined /> Solver Loop</Space>} extra={<span className="muteki-event-count">{keyMoments.length} key states</span>}>
            <StageRail phase={currentPhase} terminal={terminal} />
            <div className="muteki-key-stream">
              {keyMoments.length ? keyMoments.slice(-24).map((moment) => {
                return (
                  <div className={`muteki-key-moment is-${moment.kind} is-${moment.tone}`} key={moment.id}>
                    <div className="muteki-key-moment-head"><strong>{moment.title}</strong><span>{moment.timestamp}</span></div>
                    {moment.kind === "user" ? <div className="muteki-user-prompt">{moment.summary}</div> : <ExpandableText value={moment.summary} lines={3} className="muteki-key-moment-summary" toggleClassName="muteki-expand-toggle is-moment" />}
                    {moment.kind === "user" && <small>Blackboard · {moment.status ?? "QUEUED"}</small>}
                  </div>
                );
              }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待 Blackboard 事件" />}
            </div>
          </Card>

          <Card
            className="muteki-agent-board-card"
            title={<Space><UserOutlined /> Agent 工作台</Space>}
            extra={<span className="muteki-event-count">{activeWorkerCount} 个在跑 · 共 {workerTotal} 个</span>}
          >
            <AgentBoard workers={board.workers} />
          </Card>

          <Card
            className="muteki-case-board-card"
            title={<Space><PushpinOutlined /> 案情分析板 / Blackboard</Space>}
            extra={<span className="muteki-event-count">{caseBoard.confirmed.length} 个已确认 · {caseBoard.pending.length} 个未验证 · {[...caseBoard.confirmed, ...caseBoard.pending].reduce((total, card) => total + card.evidenceRefs.length, 0)} 条证据</span>}
          >
            <CaseBoardCanvas cards={caseBoard.confirmed} pendingCards={caseBoard.pending} fullscreen={false} positions={caseBoardLayout.positions} semantic={boardSemantic} currentRevision={mutekiState.data?.revision} expandedCards={expandedCaseCards} onToggleExpand={toggleCaseCardExpanded} onPositionChange={updateCaseBoardPosition} onArrange={arrangeCaseBoard} onAnalyze={() => semanticAnalysis.mutate()} onToggleFullscreen={() => setCaseBoardFullscreen(true)} />
          </Card>

          <Card className="muteki-input-card" title={<Space><SendOutlined /> Coordinator Input</Space>} extra={<span className="muteki-subtle">下一轮 Step 生效</span>}>
            <Space.Compact block>
              <Input value={continuation} onChange={(event) => setContinuation(event.target.value)} placeholder="补充授权范围、目标上下文或下一步约束" disabled={terminal} />
              <Button type="primary" icon={<SendOutlined />} disabled={terminal || !continuation.trim()} onClick={() => api.sendRunMessage(id, continuation).then(() => { setContinuation(""); message.success("信息已排队"); }).catch((error: Error) => message.error(error.message))}>发送</Button>
            </Space.Compact>
            <div className="muteki-input-hint">输入会进入 Blackboard 的下一次 Reason/Coordinator 循环，不会绕过 Policy 或 Completion Gate。</div>
          </Card>
        </main>

        {caseBoardFullscreen && (
          <div className="muteki-case-board-fullscreen" role="dialog" aria-modal="true" aria-label="案情分析板全屏">
            <div className="muteki-case-board-fullscreen-head">
              <div><strong>案情分析板 / Blackboard</strong><span>上方为已确认线索，下方虚线分区为未验证线索</span></div>
              <Button icon={<FullscreenExitOutlined />} onClick={() => setCaseBoardFullscreen(false)}>退出全屏</Button>
            </div>
            <CaseBoardCanvas cards={caseBoard.confirmed} pendingCards={caseBoard.pending} fullscreen positions={caseBoardLayout.positions} semantic={boardSemantic} currentRevision={mutekiState.data?.revision} expandedCards={expandedCaseCards} onToggleExpand={toggleCaseCardExpanded} onPositionChange={updateCaseBoardPosition} onArrange={arrangeCaseBoard} onAnalyze={() => semanticAnalysis.mutate()} onToggleFullscreen={() => setCaseBoardFullscreen(false)} />
          </div>
        )}

        <aside className="muteki-inspector">
          <Tabs
            className="muteki-inspector-tabs"
            defaultActiveKey="result"
            items={[
              {
                key: "result",
                label: <span><FlagFilled /> 结果 <Badge count={canonicalFlag ? 1 : flagRows.length} size="small" /></span>,
                children: (
                  <div className="muteki-inspector-body">
                    <div className={`muteki-result-banner ${canonicalFlag ? "is-solved" : ""}`}>
                      {canonicalFlag ? <CheckCircleFilled /> : <ClockCircleOutlined />}
                      <div><strong>{canonicalFlag ? "FLAG 已验证" : pendingVerifiedFlag ? "已记录验证事件" : terminal ? "运行已结束" : "等待证据"}</strong><span>{canonicalFlag ? "Completion Gate 已确认结果" : pendingVerifiedFlag ? "已收到验证事件，但当前 Run 尚未完成正式结果落盘" : health.data?.next_action ?? "Coordinator 等待下一条有效观察"}</span></div>
                    </div>
                    {!canonicalFlag && outcome?.outcome_class === "aborted_infra" && (
                      <div className="muteki-direction-card is-infra">
                        <div className="muteki-direction-head"><WarningOutlined /> 基础设施失败 · 不计入解题率</div>
                        <div className="muteki-direction-body">这不是解法问题：{outcome.infra_reasons.length ? outcome.infra_reasons.join("；") : "运行环境不可用"}。</div>
                        <small>该 Run 已排除出解题率分母；基础设施失败不产生研究方向，重跑即可。</small>
                      </div>
                    )}
                    {!canonicalFlag && direction && (
                      <div className={`muteki-direction-card is-${direction.confidence}`}>
                        <div className="muteki-direction-head">
                          <CompassOutlined /> 下一步最有价值的方向
                          <Tag color={direction.confidence === "high" ? "green" : direction.confidence === "medium" ? "gold" : "default"}>{directionLabel(direction.confidence)}</Tag>
                        </div>
                        <div className="muteki-direction-body"><ExpandableText value={direction.action} lines={4} /></div>
                        <div className="muteki-direction-rationale"><ExpandableText value={direction.rationale} lines={3} /></div>
                        {direction.evidence.length > 0 && (
                          <details className="muteki-direction-evidence">
                            <summary>依据 · {direction.evidence.length} 条（{direction.source}）</summary>
                            {direction.evidence.map((item, index) => <div key={`${index}-${item.slice(0, 12)}`}><ExpandableText value={item} lines={2} /></div>)}
                          </details>
                        )}
                      </div>
                    )}
                    {!canonicalFlag && terminal && diagnostics.isFetching && !diagnostics.data && (
                      <div className="muteki-direction-card is-quiet"><div className="muteki-direction-head"><CompassOutlined /> 正在评估下一步方向…</div></div>
                    )}
                    {canonicalFlag && <div className="muteki-canonical-flag">
                      <div className="muteki-canonical-flag-label"><CheckCircleFilled /> 已验证 Flag <Tag color="green">含证据支撑</Tag></div>
                      <Typography.Text className="muteki-canonical-flag-value" copyable={{ text: canonicalFlag }}>{canonicalFlag}</Typography.Text>
                      <small>Muteki 完成门禁的正式结果</small>
                      <Button className="muteki-poc-export" icon={<DownloadOutlined />} loading={pocExporting} onClick={exportPoc}>导出可复现 PoC 包</Button>
                    </div>}
                    {report.data?.content ? (
                      <div className="muteki-export-actions">
                        <Button className="muteki-writeup-export" icon={<DownloadOutlined />} loading={writeupExporting} onClick={exportWriteup}>导出 Writeup（Markdown）</Button>
                      </div>
                    ) : null}
                    <div className="muteki-inspector-section-title">Flag 候选</div>
                    {flagRows.length ? flagRows.map((item) => {
                      const meta = flagMeta(item.review_state);
                      return <div className="muteki-flag-row" key={item.id}><Typography.Text ellipsis={{ tooltip: item.candidate }}>{item.candidate}</Typography.Text><Space size={4}><Tag color={meta.color}>{meta.label}</Tag><Button type="text" size="small" icon={<CheckCircleFilled />} onClick={() => reviewFlag.mutate({ candidateId: item.id, reviewState: "VALID" })} /><Button danger type="text" size="small" icon={<WarningOutlined />} onClick={() => reviewFlag.mutate({ candidateId: item.id, reviewState: "INVALID" })} /></Space></div>;
                    }) : canonicalFlag ? null : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有 Flag 候选" />}
                    <div className="muteki-result-chips"><Tag color="green">已验证 {flagRows.filter((item) => item.review_state === "VALID").length}</Tag><Tag color="gold">待复核 {flagRows.filter((item) => item.review_state === "OPEN").length}</Tag><Tag color="red">已否决 {flagRows.filter((item) => item.review_state === "INVALID").length}</Tag></div>
                    <details className="muteki-report-details"><summary>打开最终报告</summary><pre>{report.data?.content ?? "报告尚未生成"}</pre></details>
                  </div>
                ),
              },
              {
                key: "blackboard",
                label: <span><NodeIndexOutlined /> Blackboard</span>,
                children: (
                    <div className="muteki-inspector-body">
                      <div className="muteki-inspector-section-title">当前认知</div>
                    {pendingVerifiedFlag && <div className="muteki-pending-flag"><div><ClockCircleOutlined /> Verified event retained</div><small>当前 Run 尚未通过最终生命周期状态，Completion Gate/report 生成后才会展示具体 Flag。</small></div>}
                    {canonicalFlag && <div className="muteki-blackboard-flag"><div><CheckCircleFilled /> Verified Flag</div><Typography.Text copyable={{ text: canonicalFlag }}>{canonicalFlag}</Typography.Text><small>Completion Gate → canonical Blackboard result</small></div>}
                    {board.facts.length ? <>
                      <div className="muteki-result-chips"><Tag color="green">已验证 {board.facts.filter((fact) => fact.verified).length}</Tag><Tag color="gold">待验证 {board.facts.filter((fact) => !fact.verified).length}</Tag></div>
                      {(showAllFacts ? board.facts : board.facts.slice(0, 8)).map((fact) => (
                        <div className="muteki-fact-row" key={`${fact.sequence}-${fact.content}`}>
                          <span className="muteki-fact-icon">{fact.verified ? <CheckCircleFilled /> : <ClockCircleOutlined />}</span>
                          <div>
                            <ExpandableText value={fact.content} lines={3} className="muteki-fact-text" />
                            <small>#{fact.sequence} · {fact.verified ? "已验证" : fact.state === "challenged" ? "待验证 · 已派验证单" : "待验证"} · Evidence {fact.evidenceCount}</small>
                          </div>
                        </div>
                      ))}
                      {board.facts.length > 8 && (
                        <button type="button" className="muteki-expand-toggle is-list" onClick={() => setShowAllFacts((current) => !current)}>
                          {showAllFacts ? "收起 · 只看前 8 条" : `显示全部 ${board.facts.length} 条事实`}
                        </button>
                      )}
                    </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="黑板暂无事实" />}
                    <div className="muteki-inspector-section-title">User Prompts → Blackboard</div>
                    {runMessages.data?.length ? runMessages.data.slice(-4).reverse().map((input) => <div className="muteki-blackboard-input" key={input.id}><div><strong>{input.content}</strong><small>revision {input.revision} · {input.status}</small></div></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无用户提示" />}
                    <div className="muteki-inspector-section-title">Intent Queue</div>
                    {board.intents.length ? board.intents.slice(0, 8).map((intent) => <div className="muteki-intent-row" key={intent.id}><Space size={5} wrap><Tag color={statusColor(intent.status)}>{statusLabel(intent.status)}</Tag>{intent.tool && <Tag>{intent.tool}</Tag>}{intent.classification && <Tag color="purple">{intent.classification}</Tag>}</Space><ExpandableText value={intent.description} lines={2} className="muteki-intent-text" /><small className="is-wrap">{intent.worker ? `Worker ${intent.worker}` : "等待认领"}{intent.result ? ` · ${intent.result}` : ""}</small></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Intent" />}
                    <div className="muteki-context-grid"><Statistic title="无进展" value={solverState.data?.no_progress_count ?? 0} /><Statistic title="Review" value={board.reviewCount} /><Statistic title="当前阶段" value={currentPhase.toUpperCase()} /></div>
                  </div>
                ),
              },
              {
                key: "workers",
                label: <span><UserOutlined /> Workers</span>,
                children: (
                  <div className="muteki-inspector-body">
                    <div className="muteki-inspector-section-title">Worker Pool</div>
                    {board.workers.length ? board.workers.map((worker) => <div className="muteki-worker-row" key={worker.id}><Badge status={worker.status === "running" ? "processing" : "default"} /><div><strong>{worker.role}</strong><small>{worker.engine} · {worker.status === "running" ? "活动中" : "已结束"}{worker.intentId ? ` · ${worker.intentId}` : ""}</small></div></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Worker" />}
                    <div className="muteki-inspector-section-title">Health</div>
                    <div className="muteki-health-list"><div><span>下一步</span><strong>{health.data?.next_action ?? "—"}</strong></div><div><span>最近工具</span><strong>{health.data?.progress.last_tool ?? "—"}</strong></div><div><span>错误</span><strong>{health.data?.last_error_code ?? "—"}</strong></div></div>
                    <div className="muteki-inspector-section-title">Events</div><div className="muteki-event-mini-count">{events.length} 条审计事件实时接收中</div>
                  </div>
                ),
              },
              {
                key: "evidence",
                label: <span><FileSearchOutlined /> Evidence</span>,
                children: (
                  <div className="muteki-inspector-body">
                    <div className="muteki-inspector-section-title">Artifacts</div>
                    {artifacts.data?.length ? artifacts.data.slice(0, 8).map((item) => <div className="muteki-artifact-row" key={item.id}><div><strong>{item.path}</strong><small>{item.type} · {item.size} bytes</small></div><Button type="text" size="small" icon={<EyeOutlined />} onClick={() => api.getArtifact(id, item.id).then(setArtifactContent).catch((error: Error) => message.error(error.message))} /></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Evidence Artifact" />}
                    <div className="muteki-inspector-section-title">Observations</div><div className="muteki-observation-count"><ExperimentOutlined /> {observations.data?.length ?? 0} 条观察摘要</div>
                    <div className="muteki-inspector-section-title">Tool Calls</div><Table className="muteki-mini-table" size="small" rowKey="id" dataSource={(tools.data ?? []).slice(-6).reverse()} pagination={false} columns={[{ title: "工具", dataIndex: "tool_name" }, { title: "状态", dataIndex: "status", render: (value: unknown) => <Tag color={String(value) === "COMPLETED" ? "green" : "gold"}>{String(value ?? "—")}</Tag> }]} locale={{ emptyText: "暂无调用" }} />
                  </div>
                ),
              },
            ]}
          />
        </aside>
      </div>

      <Modal open={Boolean(artifactContent)} title={artifactContent?.path} footer={<Button onClick={() => setArtifactContent(undefined)}>关闭</Button>} onCancel={() => setArtifactContent(undefined)}><pre className="muteki-report-pre">{artifactContent?.content}</pre></Modal>
    </div>
  );
}
