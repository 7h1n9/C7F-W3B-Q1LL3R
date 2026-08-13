import {
  ApartmentOutlined,
  CaretRightOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
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
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { useParams } from "react-router-dom";
import { RunStatusTag } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { FlagCandidate, MutekiBoardSemantic, MutekiGraphState, RunEvent, RunUserInput } from "../types/api";
import { isTerminalRunStatus, solverModeLabel } from "../utils/run";

type EventPayload = Record<string, unknown>;
type NormalizedEvent = { type: string; actor: string; payload: EventPayload; verified: boolean };
type WorkerState = { id: string; role: string; engine: string; status: "running" | "finished"; intentId?: string };
type IntentState = { id: string; description: string; status: "open" | "claimed" | "done"; worker?: string; tool?: string; classification?: string; result?: string };
type FactState = { sequence: number; content: string; verified: boolean; evidenceCount: number };
type BoardState = { phase: string; flagFound: boolean; workers: WorkerState[]; intents: IntentState[]; facts: FactState[]; reviewCount: number };
type CaseBoardCard = {
  id: string;
  kind: "fact" | "finding" | "flag";
  content: string;
  sourceContent?: string;
  verified: boolean;
  evidenceRefs: string[];
  source?: string;
  sequence: number;
};
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

function clip(value: string, size = 150): string {
  return value.length > size ? `${value.slice(0, size)}…` : value;
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

function eventKey(event: RunEvent): string {
  return event.event_type.startsWith("muteki.") ? event.event_type : event.event_type;
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
  if (type === "intent_proposed") return clip(text(payload.description, "新 Intent"));
  if (type === "intent_claimed" || type === "intent_released" || type === "intent_concluded") return `${text(payload.intent_id, "intent")} ${text(payload.result, "")}`.trim();
  if (type === "fact_added") return clip(text(payload.content, "写入一条事实"));
  if (type === "coordinator_directive" || type === "operator_directive" || type === "intent_state_changed") {
    return [text(payload.directive, text(payload.state, type)), text(payload.reason_code, text(payload.reason))].filter(Boolean).join(" · ");
  }
  if (type === "tool.requested" || type === "tool.started" || type === "tool.completed" || type === "tool.failed") {
    const fallbackStatus = type.split(".")[type.split(".").length - 1] ?? "更新";
    return `${tool || "tool"} · ${text(payload.status, fallbackStatus)}`;
  }
  if (type === "artifact.created") return `${text(payload.path, "artifact")} · ${text(payload.size, "")}`.trim();
  if (type === "review_finding" || type === "review_proposal" || type === "review_proposal_decision") return clip(text(payload.reason ?? payload.label ?? payload.decision, "Review 更新"));
  if (type === "branch_split" || type === "branch_resolved" || type === "route_suppressed" || type === "route_reopened") return clip(text(payload.reason ?? payload.route_hash ?? type));
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
): string | undefined {
  if (runStatus !== "COMPLETED_SOLVED" || reportJson?.flag_verified !== true) return undefined;
  const value = reportJson.flag;
  return typeof value === "string" && /^flag\{[^{}\r\n]+\}$/i.test(value.trim()) ? value.trim() : undefined;
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
      tone: eventTone(eventKey(event)),
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

function formatDuration(startedAt?: string | null, finishedAt?: string | null): string {
  if (!startedAt) return "—";
  const start = Date.parse(startedAt);
  const finish = finishedAt ? Date.parse(finishedAt) : Date.now();
  if (Number.isNaN(start) || Number.isNaN(finish) || finish < start) return "—";
  const total = Math.floor((finish - start) / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return `${hours ? `${hours}:` : ""}${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function projectBoard(events: RunEvent[], nativeState?: MutekiGraphState): BoardState {
  const workers = new Map<string, WorkerState>();
  const intents = new Map<string, IntentState>();
  const facts: FactState[] = [];
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

    if (type === "worker_started" || type === "worker_step" || type === "worker_finished") {
      const workerId = text(payload.worker_id, actor);
      const previous = workers.get(workerId);
      workers.set(workerId, {
        id: workerId,
        role: text(payload.role, previous?.role ?? "worker"),
        engine: text(payload.engine_id, previous?.engine ?? "—"),
        status: type === "worker_finished" ? "finished" : "running",
        intentId: text(payload.intent_id, previous?.intentId),
      });
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
      facts.push({ sequence: event.sequence, content: text(payload.content, "未命名事实"), verified: Boolean(payload.verified ?? verified), evidenceCount: refs.length });
    }
  }

  // The official SharedGraph is the Muteki Blackboard authority.  MySQL SSE
  // events remain useful for live Worker/audit detail, but they are bounded
  // and can arrive after a reconnect.  Prefer the read-only native snapshot
  // for facts/intents/flags whenever it is available so the Workspace cannot
  // display an empty Blackboard while the run-scoped graph is populated.
  if (nativeState?.available) {
    return {
      phase,
      flagFound: flagFound || nativeState.flags.some((item) => item.verified),
      workers: [...workers.values()].reverse(),
      intents: [...nativeState.intents].sort((left, right) => (right.sequence ?? 0) - (left.sequence ?? 0)).map((intent) => ({
        id: intent.id,
        description: intent.description || "未命名 Intent",
        status: intent.status,
        worker: intent.worker ?? undefined,
        result: intent.result,
      })),
      facts: [...nativeState.facts].sort((left, right) => right.sequence - left.sequence).map((fact) => ({
        sequence: fact.sequence,
        content: fact.content || "未命名事实",
        verified: fact.verified,
        evidenceCount: fact.evidence_refs.length,
      })),
      reviewCount,
    };
  }
  return { phase, flagFound, workers: [...workers.values()].reverse(), intents: [...intents.values()].reverse(), facts: facts.reverse(), reviewCount };
}

function projectCaseBoard(nativeState: MutekiGraphState | undefined, fallback: BoardState, canonicalFlag?: string): CaseBoardCard[] {
  const cards: CaseBoardCard[] = [];
  const semanticByCard = new Map((nativeState?.board_semantic?.items ?? []).map((item) => [item.card_id, item]));
  const facts: Array<{ sequence: number; content: string; summary_zh?: string; verified: boolean; evidence_refs: string[]; source_worker_id?: string }> = nativeState?.available ? (nativeState.key_conditions ?? []) : fallback.facts.filter((fact) => fact.verified && fact.evidenceCount > 0).map((fact) => ({
    sequence: fact.sequence,
    content: fact.content,
    summary_zh: fact.content,
    verified: fact.verified,
    evidence_refs: Array.from({ length: fact.evidenceCount }, (_, index) => `evidence-${index + 1}`),
  }));
  for (const fact of facts) {
    const content = text(fact.content).trim();
    if (!content) continue;
    cards.push({
      id: `fact-${fact.sequence}-${content}`,
      kind: /flag|password|credential|账号|密码|token/i.test(content) ? "finding" : "fact",
      content: text(semanticByCard.get(`fact-${fact.sequence}`)?.summary_zh, text(fact.summary_zh, `已确认条件：${content}`)),
      sourceContent: content,
      verified: fact.verified,
      evidenceRefs: fact.evidence_refs ?? [],
      source: fact.source_worker_id,
      sequence: fact.sequence,
    });
  }
  for (const flag of nativeState?.flags ?? []) {
    if (!flag.flag || !flag.verified || !flag.evidence_refs.length) continue;
    cards.push({
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
    cards.push({
      id: `poc-${poc.poc_id}`,
      kind: "finding",
      content: `PoC: ${label}${poc.note ? ` — ${poc.note}` : ""}`,
      verified: poc.status !== "rejected",
      evidenceRefs: poc.artifact_id ? [poc.artifact_id] : [],
      source: "Muteki PoC",
      sequence: cards.length + 1,
    });
  }
  if (canonicalFlag && !cards.some((card) => card.kind === "flag" && card.content === canonicalFlag)) {
    cards.push({ id: "canonical-flag", kind: "flag", content: canonicalFlag, verified: true, evidenceRefs: [], source: "Completion Gate", sequence: cards.length + 1 });
  }
  return cards.sort((left, right) => left.sequence - right.sequence);
}

function CaseBoardCanvas({ cards, fullscreen, positions, semantic, currentRevision, onPositionChange, onArrange, onAnalyze, onToggleFullscreen }: {
  cards: CaseBoardCard[];
  fullscreen: boolean;
  positions: Record<string, BoardPosition>;
  semantic?: MutekiBoardSemantic;
  currentRevision?: number;
  onPositionChange: (cardId: string, position: BoardPosition) => void;
  onArrange: () => void;
  onAnalyze: () => void;
  onToggleFullscreen: () => void;
}) {
  const semanticCurrent = semantic?.status === "completed" && semantic.revision === currentRevision;
  const boardRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ cardId: string; offsetX: number; offsetY: number; width: number; height: number }>();

  const beginDrag = (event: ReactPointerEvent<HTMLElement>, cardId: string) => {
    const board = boardRef.current;
    if (!board) return;
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
    const board = boardRef.current;
    if (!drag || !board) return;
    const bounds = board.getBoundingClientRect();
    const maxX = Math.max(0, bounds.width - drag.width - 8);
    const maxY = Math.max(0, bounds.height - drag.height - 8);
    const x = Math.max(0, Math.min(maxX, event.clientX - bounds.left - drag.offsetX));
    const y = Math.max(0, Math.min(maxY, event.clientY - bounds.top - drag.offsetY));
    onPositionChange(drag.cardId, { x: (x / Math.max(1, bounds.width)) * 100, y: (y / Math.max(1, bounds.height)) * 100 });
  };

  const endDrag = () => { dragRef.current = undefined; };

  return (
    <>
      <div className="muteki-case-board-toolbar">
        <div className="muteki-case-board-legend">
          <span><i className="is-fact" /> 已确认事实</span>
          <span><i className="is-finding" /> 关键发现</span>
          <span><i className="is-verified" /> 有证据支撑</span>
        </div>
        <Space size={4}>
          <Button size="small" type="text" onClick={onArrange}>自动整理</Button>
          <Button size="small" type="text" loading={semantic?.status === "running"} disabled={!cards.length || semantic?.status === "running" || semanticCurrent} onClick={onAnalyze}>
            {semanticCurrent ? "Codex 已解析" : semantic?.status === "completed" ? "重新解析" : semantic?.status === "failed" ? "重试 AI 解析" : "AI 解析（Codex）"}
          </Button>
          <Button size="small" type="text" icon={fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />} onClick={onToggleFullscreen}>
            {fullscreen ? "退出全屏" : "全屏查看"}
          </Button>
        </Space>
      </div>
      <div
        ref={boardRef}
        className={`muteki-case-board ${fullscreen ? "is-fullscreen-board" : ""}`}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        {cards.length ? cards.map((card) => (
          <article
            className={`muteki-case-note is-${card.kind} ${card.verified ? "is-verified" : "is-pending"}`}
            key={card.id}
            data-board-card-id={card.id}
            style={{ left: `${(positions[card.id] ?? defaultBoardPosition(cards.indexOf(card))).x}%`, top: `${(positions[card.id] ?? defaultBoardPosition(cards.indexOf(card))).y}%` }}
            onPointerDown={(event) => beginDrag(event, card.id)}
            title={card.sourceContent && card.sourceContent !== card.content ? `原始事实：${card.sourceContent}` : card.content}
          >
            <div className="muteki-case-note-head"><strong>{card.kind === "flag" ? "最终答案" : card.kind === "finding" ? "关键发现" : "已确认事实"}</strong><span>#{card.sequence}</span></div>
            <div className="muteki-case-note-content">{clip(card.content, 220)}</div>
            <small>已验证 · 证据 {card.evidenceRefs.length} 条{card.source ? ` · 来源 ${card.source}` : ""}</small>
          </article>
        )) : <div className="muteki-case-board-empty"><PushpinOutlined /><span>等待 Blackboard 写入已确认条件</span><small>只显示有效、已验证且具备证据引用的关键事实</small></div>}
      </div>
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
  const [caseBoardFullscreen, setCaseBoardFullscreen] = useState(false);
  const [caseBoardLayout, setCaseBoardLayout] = useState<BoardLayout>(() => readBoardLayout(id));

  useEffect(() => {
    setCaseBoardLayout(readBoardLayout(id));
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
  const report = useQuery({ queryKey: ["report", id], queryFn: () => api.getReport(id), retry: false });
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
    () => verifiedCanonicalFlag(run.data?.status, report.data?.report_json),
    [run.data?.status, report.data?.report_json],
  );
  const caseBoard = useMemo(() => projectCaseBoard(mutekiState.data, board, canonicalFlag), [board, canonicalFlag, mutekiState.data]);
  const semanticAnalysis = useMutation({
    mutationFn: () => api.startMutekiBoardSemanticAnalysis(id),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["muteki-state", id] });
      if (result.status === "running") message.success("Codex 语义解析已在后台开始，解题流程不会中断");
      else if (result.status === "completed") message.success("分析板语义解析已完成");
    },
    onError: (error: Error) => message.error(error.message),
  });
  const updateCaseBoardPosition = (cardId: string, position: BoardPosition) => {
    setCaseBoardLayout((current) => ({ runId: id, positions: { ...current.positions, [cardId]: position } }));
  };
  const arrangeCaseBoard = () => {
    const positions: Record<string, BoardPosition> = {};
    caseBoard.forEach((card, index) => { positions[card.id] = defaultBoardPosition(index); });
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
      const result = await api.downloadMutekiPoc(id);
      const url = URL.createObjectURL(new Blob([result.content], { type: "text/markdown;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      message.success("答案 PoC 已导出");
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setPocExporting(false);
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
                    {moment.kind === "user" ? <div className="muteki-user-prompt">{moment.summary}</div> : <div className="muteki-key-moment-summary">{moment.summary}</div>}
                    {moment.kind === "user" && <small>Blackboard · {moment.status ?? "QUEUED"}</small>}
                  </div>
                );
              }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待 Blackboard 事件" />}
            </div>
          </Card>

          <Card
            className="muteki-case-board-card"
            title={<Space><PushpinOutlined /> 案情分析板 / Blackboard</Space>}
            extra={<span className="muteki-event-count">{caseBoard.length} 个关键条件 · {caseBoard.reduce((total, card) => total + card.evidenceRefs.length, 0)} 条证据</span>}
          >
            <CaseBoardCanvas cards={caseBoard} fullscreen={false} positions={caseBoardLayout.positions} semantic={boardSemantic} currentRevision={mutekiState.data?.revision} onPositionChange={updateCaseBoardPosition} onArrange={arrangeCaseBoard} onAnalyze={() => semanticAnalysis.mutate()} onToggleFullscreen={() => setCaseBoardFullscreen(true)} />
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
              <div><strong>案情分析板 / Blackboard</strong><span>只显示已确认、可追溯的关键条件</span></div>
              <Button icon={<FullscreenExitOutlined />} onClick={() => setCaseBoardFullscreen(false)}>退出全屏</Button>
            </div>
            <CaseBoardCanvas cards={caseBoard} fullscreen positions={caseBoardLayout.positions} semantic={boardSemantic} currentRevision={mutekiState.data?.revision} onPositionChange={updateCaseBoardPosition} onArrange={arrangeCaseBoard} onAnalyze={() => semanticAnalysis.mutate()} onToggleFullscreen={() => setCaseBoardFullscreen(false)} />
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
                      <div><strong>{canonicalFlag ? "FLAG VERIFIED" : pendingVerifiedFlag ? "FLAG EVENT RECORDED" : terminal ? "RUN FINISHED" : "WAITING FOR EVIDENCE"}</strong><span>{canonicalFlag ? "Completion Gate 已确认结果" : pendingVerifiedFlag ? "已收到验证事件，但当前 Run 尚未完成正式结果落盘" : health.data?.next_action ?? "Coordinator 等待下一条有效观察"}</span></div>
                    </div>
                    {canonicalFlag && <div className="muteki-canonical-flag">
                      <div className="muteki-canonical-flag-label"><CheckCircleFilled /> Verified Flag <Tag color="green">Evidence-backed</Tag></div>
                      <Typography.Text className="muteki-canonical-flag-value" copyable={{ text: canonicalFlag }}>{canonicalFlag}</Typography.Text>
                      <small>Canonical Muteki Completion result</small>
                      <Button className="muteki-poc-export" icon={<DownloadOutlined />} loading={pocExporting} onClick={exportPoc}>导出答案 PoC</Button>
                    </div>}
                    <div className="muteki-inspector-section-title">Flag Candidates</div>
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
                    {board.facts.length ? board.facts.slice(0, 8).map((fact) => <div className="muteki-fact-row" key={`${fact.sequence}-${fact.content}`}><span className="muteki-fact-icon">{fact.verified ? <CheckCircleFilled /> : <ClockCircleOutlined />}</span><div><Typography.Text>{clip(fact.content, 120)}</Typography.Text><small>#{fact.sequence} · {fact.verified ? "已验证" : "待验证"} · Evidence {fact.evidenceCount}</small></div></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="黑板暂无事实" />}
                    <div className="muteki-inspector-section-title">User Prompts → Blackboard</div>
                    {runMessages.data?.length ? runMessages.data.slice(-4).reverse().map((input) => <div className="muteki-blackboard-input" key={input.id}><div><strong>{input.content}</strong><small>revision {input.revision} · {input.status}</small></div></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无用户提示" />}
                    <div className="muteki-inspector-section-title">Intent Queue</div>
                    {board.intents.length ? board.intents.slice(0, 8).map((intent) => <div className="muteki-intent-row" key={intent.id}><Space size={5} wrap><Tag color={statusColor(intent.status)}>{statusLabel(intent.status)}</Tag>{intent.tool && <Tag>{intent.tool}</Tag>}{intent.classification && <Tag color="purple">{intent.classification}</Tag>}</Space><Typography.Text>{clip(intent.description, 110)}</Typography.Text><small>{intent.worker ? `Worker ${intent.worker}` : "等待认领"}{intent.result ? ` · ${clip(intent.result, 70)}` : ""}</small></div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Intent" />}
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
