import {
  CodeOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Modal,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableProps } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../services/api";
import type {
  KnowledgeExperimentDetail,
  KnowledgeExperimentSummary,
} from "../types/api";

const { Paragraph, Text, Title } = Typography;

type JsonObject = Record<string, unknown>;

const asObject = (value: unknown): JsonObject =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};

const asNumber = (value: unknown): number | null => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
};

const asText = (value: unknown, fallback = "—"): string =>
  typeof value === "string" && value.trim() ? value : fallback;

const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const nullableText = (value: unknown): string | null =>
  typeof value === "string" && value.trim() ? value.trim() : null;

const hasObjectKeys = (value: unknown): boolean =>
  Object.keys(asObject(value)).length > 0;

const firstValueFrom = (
  objects: JsonObject[],
  keys: string[],
): unknown => {
  for (const object of objects) {
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(object, key)) {
        const value = object[key];
        if (value !== null && value !== undefined && value !== "") return value;
      }
    }
  }
  return undefined;
};

const firstTextFrom = (objects: JsonObject[], keys: string[]): string | null =>
  nullableText(firstValueFrom(objects, keys));

const formatTime = (value: unknown): string => {
  const text = asText(value, "");
  if (!text) return "—";
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime())
    ? text
    : parsed.toLocaleString("zh-CN", { hour12: false });
};

const formatDuration = (value: unknown): string => {
  const seconds = asNumber(value);
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds.toFixed(2)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${(seconds % 60).toFixed(1)} 秒`;
};

const statusColor = (status: string): string => {
  if (["SUCCESS", "COMPLETED", "COMPLETED_SOLVED", "READY"].includes(status)) {
    return "blue";
  }
  if (["RUNNING", "PLANNED", "PENDING", "QUEUED"].includes(status)) {
    return "processing";
  }
  if (["FAILED", "ERROR", "EXECUTION_ERROR"].includes(status)) return "error";
  if (["DRY_RUN", "DESCRIPTIVE_ONLY"].includes(status)) return "warning";
  return "default";
};

type ExperimentSourceKind = "db_pair" | "file" | "unknown";

interface ExperimentSource {
  kind: ExperimentSourceKind;
  label: string;
  color: string;
}

interface ExperimentArmMetadata {
  factor: string | null;
  baselineValue: string | null;
  treatmentValue: string | null;
  baselineRunId: string | null;
  treatmentRunId: string | null;
  hasArmFields: boolean;
}

interface NormalizedRunArm {
  role: "baseline" | "treatment";
  runId: string | null;
  knowledgeMode: string | null;
  status: string | null;
  finalResult: string | null;
  strictSuccess: boolean | null;
  startedAt: string | null;
  completedAt: string | null;
  error: string | null;
  raw: JsonObject;
}

const experimentContainers = (record: unknown): JsonObject[] => {
  const raw = asObject(record);
  return [
    raw,
    asObject(raw.ab_experiment),
    asObject(raw.pair),
    asObject(raw.metadata),
    asObject(raw.summary),
  ];
};

const experimentSource = (record: unknown): ExperimentSource => {
  const containers = experimentContainers(record);
  const marker = firstTextFrom(containers, [
    "source",
    "source_kind",
    "kind",
    "record_type",
    "experiment_type",
    "experiment_source",
    "origin",
    "storage",
    "type",
  ]);
  const normalizedMarker = marker?.toLowerCase().replace(/[\s-]+/g, "_");

  if (
    normalizedMarker &&
    ["db", "database", "paired_run", "run_pair"].some((part) =>
      normalizedMarker.includes(part),
    )
  ) {
    return { kind: "db_pair", label: "DB 配对", color: "geekblue" };
  }
  if (
    normalizedMarker &&
    ["file", "manifest", "disk", "artifact"].some((part) =>
      normalizedMarker.includes(part),
    )
  ) {
    return { kind: "file", label: "文件实验", color: "cyan" };
  }

  const raw = asObject(record);
  const hasRunSignals =
    asArray(raw.runs).length > 0 ||
    asArray(raw.paired_runs).length > 0 ||
    hasObjectKeys(raw.baseline) ||
    hasObjectKeys(raw.treatment) ||
    firstTextFrom(containers, [
      "baseline_run_id",
      "treatment_run_id",
      "paired_run_id",
      "baseline_value",
      "treatment_value",
    ]) !== null;
  if (hasRunSignals) {
    return { kind: "db_pair", label: "DB 配对", color: "geekblue" };
  }

  const hasFileSignals =
    hasObjectKeys(raw.manifest) ||
    hasObjectKeys(raw.plan) ||
    asArray(raw.artifacts).length > 0 ||
    nullableText(raw.path) !== null;
  if (hasFileSignals) {
    return { kind: "file", label: "文件实验", color: "cyan" };
  }

  return { kind: "unknown", label: "来源未标记", color: "default" };
};

const experimentArmMetadata = (record: unknown): ExperimentArmMetadata => {
  const containers = experimentContainers(record);
  const factor = firstTextFrom(containers, [
    "factor",
    "experiment_factor",
    "knowledge_factor",
  ]);
  const baselineValue = firstTextFrom(containers, [
    "baseline_value",
    "baseline_knowledge_mode",
    "baseline_mode",
    "off_value",
  ]);
  const treatmentValue = firstTextFrom(containers, [
    "treatment_value",
    "treatment_knowledge_mode",
    "treatment_mode",
    "on_value",
  ]);
  const baselineRunId = firstTextFrom(containers, [
    "baseline_run_id",
    "off_run_id",
    "control_run_id",
  ]);
  const treatmentRunId = firstTextFrom(containers, [
    "treatment_run_id",
    "on_run_id",
  ]);

  return {
    factor,
    baselineValue,
    treatmentValue,
    baselineRunId,
    treatmentRunId,
    hasArmFields: [
      factor,
      baselineValue,
      treatmentValue,
      baselineRunId,
      treatmentRunId,
    ].some(Boolean),
  };
};

const collectRunCandidates = (record: unknown): JsonObject[] => {
  const containers = experimentContainers(record);
  const candidates: JsonObject[] = [];

  for (const container of containers) {
    for (const key of ["runs", "paired_runs", "experiment_runs", "arms"]) {
      const values = asArray(container[key]);
      for (const value of values) {
        const candidate = asObject(value);
        if (hasObjectKeys(candidate)) candidates.push(candidate);
      }
    }

    const arms = asObject(container.arms);
    for (const value of Object.values(arms)) {
      const candidate = asObject(value);
      if (hasObjectKeys(candidate)) candidates.push(candidate);
    }

    for (const key of [
      "baseline",
      "baseline_run",
      "control",
      "control_run",
      "treatment",
      "treatment_run",
    ]) {
      const candidate = asObject(container[key]);
      if (hasObjectKeys(candidate)) candidates.push(candidate);
    }
  }

  return candidates;
};

const runRole = (run: JsonObject): string =>
  (
    firstTextFrom([run], ["role", "arm_role", "group", "variant", "label"]) ??
    asText(run.arm, "")
  ).toLowerCase();

const runKnowledgeMode = (run: JsonObject): string | null =>
  firstTextFrom([run], [
    "knowledge_mode",
    "mode",
    "factor_value",
    "arm_value",
    "value",
  ]);

const runMatchesRole = (
  run: JsonObject,
  role: NormalizedRunArm["role"],
  metadata: ExperimentArmMetadata,
): boolean => {
  const roleHint = runRole(run);
  const mode = runKnowledgeMode(run);
  const expectedValue =
    role === "baseline" ? metadata.baselineValue : metadata.treatmentValue;

  if (role === "baseline") {
    if (["baseline", "control", "off"].some((part) => roleHint.includes(part))) {
      return true;
    }
  } else if (
    ["treatment", "treated", "on"].some((part) => roleHint.includes(part))
  ) {
    return true;
  }

  if (expectedValue && mode === expectedValue) return true;
  return (
    (role === "baseline" && mode?.toLowerCase() === "off") ||
    (role === "treatment" && mode?.toLowerCase() === "on")
  );
};

const normalizeRunArm = (
  value: unknown,
  role: NormalizedRunArm["role"],
  metadata: ExperimentArmMetadata,
): NormalizedRunArm => {
  const arm = asObject(value);
  const run = {
    ...arm,
    ...asObject(arm.run),
    ...asObject(arm.solve_run),
  };
  const fallbackRunId =
    role === "baseline" ? metadata.baselineRunId : metadata.treatmentRunId;
  const fallbackMode =
    role === "baseline" ? metadata.baselineValue : metadata.treatmentValue;
  const evaluationReport = asObject(
    run.evaluation_report ?? run.evaluator_report ?? run.evaluation,
  );
  const finalResult = firstTextFrom(
    [evaluationReport, run],
    ["final_result", "evaluator_final_result"],
  );

  return {
    role,
    runId:
      firstTextFrom([run], ["run_id", "solve_run_id", "id"]) ??
      fallbackRunId,
    knowledgeMode: runKnowledgeMode(run) ?? fallbackMode,
    status: firstTextFrom([run], [
      "terminal_status",
      "run_status",
      "lifecycle_status",
      "status",
    ]),
    finalResult,
    strictSuccess: finalResult === null ? null : finalResult === "SUCCESS",
    startedAt: firstTextFrom([run], [
      "started_at",
      "start_time",
      "created_at",
    ]),
    completedAt: firstTextFrom([run], [
      "completed_at",
      "finished_at",
      "ended_at",
      "updated_at",
    ]),
    error: firstTextFrom([run], [
      "error",
      "error_message",
      "failure_error",
      "failure_reason",
      "last_error",
    ]),
    raw: run,
  };
};

const pairedRunArms = (
  record: unknown,
): { baseline: NormalizedRunArm; treatment: NormalizedRunArm } => {
  const raw = asObject(record);
  const metadata = experimentArmMetadata(record);
  const candidates = collectRunCandidates(record);
  const explicitBaseline =
    raw.baseline ?? raw.baseline_run ?? raw.control ?? raw.control_run;
  const explicitTreatment = raw.treatment ?? raw.treatment_run;
  const baselineCandidate =
    (hasObjectKeys(explicitBaseline) ? explicitBaseline : undefined) ??
    candidates.find((run) => runMatchesRole(run, "baseline", metadata));
  const treatmentCandidate =
    (hasObjectKeys(explicitTreatment) ? explicitTreatment : undefined) ??
    candidates.find((run) => runMatchesRole(run, "treatment", metadata));

  return {
    baseline: normalizeRunArm(
      baselineCandidate ?? candidates[0],
      "baseline",
      metadata,
    ),
    treatment: normalizeRunArm(
      treatmentCandidate ??
        candidates.find((run) => run !== baselineCandidate) ??
        candidates[1],
      "treatment",
      metadata,
    ),
  };
};

const experimentTime = (record: KnowledgeExperimentSummary): string => {
  const raw = asObject(record);
  const manifest = asObject(record.manifest);
  return formatTime(
    raw.created_at ??
      raw.updated_at ??
      manifest.created_at ??
      asObject(record.plan).created_at,
  );
};

const pairedEffect = (record: KnowledgeExperimentSummary): JsonObject =>
  asObject(record.paired_effect);

const hasDescriptiveOnlyMarker = (value: unknown): boolean =>
  typeof value === "string" && value.toLowerCase().includes("descriptive_only");

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre
      style={{
        margin: 0,
        maxHeight: 520,
        overflow: "auto",
        padding: 16,
        border: "1px solid rgba(127, 127, 127, 0.22)",
        borderRadius: 8,
        background: "rgba(0, 0, 0, 0.18)",
        fontSize: 12,
        lineHeight: 1.55,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      {JSON.stringify(value ?? {}, null, 2)}
    </pre>
  );
}

function JsonSection({ title, value }: { title: string; value: unknown }) {
  return (
    <Card
      size="small"
      title={title}
      style={{ marginTop: 16 }}
      extra={<Text type="secondary">只读原始审计数据</Text>}
    >
      <JsonBlock value={value} />
    </Card>
  );
}

function ArmCard({
  arm,
  value,
  label,
}: {
  arm: string;
  value: unknown;
  label?: string;
}) {
  const stats = asObject(value);
  const attempts = asNumber(stats.attempts) ?? 0;
  const solved = asNumber(stats.success) ?? 0;
  const errors = asNumber(stats.errors) ?? 0;
  const evaluated = asNumber(stats.evaluated_attempts);
  const hasEvaluatorEvidence = evaluated !== null && evaluated > 0;
  const strictSuccess = hasEvaluatorEvidence ? solved : null;
  const normalizedArm = arm.toLowerCase();
  const displayArm = arm.toUpperCase();
  const fallbackLabel =
    normalizedArm === "on" || normalizedArm === "treatment"
      ? "启用知识先验"
      : normalizedArm === "off" || normalizedArm === "baseline"
        ? "关闭知识先验"
        : arm;

  return (
    <Card
      size="small"
      title={
        <Space>
          <Tag color={normalizedArm === "on" ? "blue" : "default"}>
            {displayArm}
          </Tag>
          <span>{label ?? fallbackLabel}</span>
        </Space>
      }
    >
      <Row gutter={[12, 12]}>
        <Col xs={12} sm={6}>
          <Statistic title="执行数" value={attempts} />
        </Col>
        <Col xs={12} sm={6}>
          <Statistic
            title="evaluator-backed 成功"
            value={strictSuccess ?? "未评估/不可判定"}
            valueStyle={{
              color: strictSuccess !== null && strictSuccess > 0
                ? "#52c41a"
                : undefined,
            }}
          />
        </Col>
        <Col xs={12} sm={6}>
          <Statistic
            title="错误数"
            value={errors}
            valueStyle={{ color: errors > 0 ? "#ff4d4f" : undefined }}
          />
        </Col>
        <Col xs={12} sm={6}>
          <Statistic
            title="含评估报告"
            value={evaluated ?? "未提供"}
          />
        </Col>
      </Row>
      <Descriptions
        size="small"
        column={1}
        style={{ marginTop: 16 }}
        items={[
          {
            key: "duration",
            label: "平均耗时",
            children: formatDuration(stats.average_duration_seconds),
          },
          {
            key: "tool-calls",
            label: "平均工具调用",
            children:
              asNumber(stats.average_tool_calls)?.toFixed(2) ?? "—",
          },
          {
            key: "statuses",
            label: "Run 状态分布",
            children: (
              <Space wrap size={[4, 4]}>
                {Object.entries(asObject(stats.final_status_distribution)).length ? (
                  Object.entries(asObject(stats.final_status_distribution)).map(
                    ([status, count]) => (
                      <Tag key={status}>
                        {status}: {String(count)}
                      </Tag>
                    ),
                  )
                ) : (
                  "—"
                )}
              </Space>
            ),
          },
        ]}
      />
    </Card>
  );
}

function ExperimentSourceTag({ record }: { record: unknown }) {
  const source = experimentSource(record);
  return <Tag color={source.color}>{source.label}</Tag>;
}

function PairedEffectPanel({
  record,
  summary,
}: {
  record: unknown;
  summary: JsonObject;
}) {
  const raw = asObject(record);
  const paired = asObject(
    summary.paired_effect ?? raw.paired_effect,
  );
  const hasPairedEffect = hasObjectKeys(paired);
  const sampleCount =
    asNumber(paired.sample_count) ?? asNumber(paired.pairs) ?? 0;
  const minimum = asNumber(paired.minimum_recommended_samples) ?? 10;
  const descriptiveOnly =
    !hasPairedEffect ||
    sampleCount < minimum ||
    paired.sample_adequate === false ||
    hasDescriptiveOnlyMarker(paired.claim) ||
    hasDescriptiveOnlyMarker(paired.warning) ||
    hasDescriptiveOnlyMarker(summary.claim) ||
    hasDescriptiveOnlyMarker(summary.warning);
  const delta =
    asNumber(paired.success_delta) ??
    asNumber(paired.on_minus_off_success_delta);

  return (
    <>
      {!hasPairedEffect || sampleCount < minimum ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message="当前结果仅可描述，不能据此宣称效果提升"
          description={
            !hasPairedEffect
              ? "后端尚未提供 paired_effect；配对样本不足或聚合仍在进行时，页面不展示效果提升结论。"
              : `配对样本 ${sampleCount} 个，少于建议的 ${minimum} 个；仅标记 descriptive_only，并隐藏效果性差值。`
          }
        />
      ) : (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 12 }}
          message="样本量达到当前展示门槛"
          description="这里仍是描述性 A/B 对比，不等同于统计显著性、因果关系或题型泛化结论。"
        />
      )}
      <Descriptions
        bordered
        size="small"
        column={{ xs: 1, sm: 1, md: 2 }}
        style={{ marginTop: 16 }}
        items={[
          {
            key: "paired-count",
            label: "配对样本",
            children: hasPairedEffect ? sampleCount : "未提供",
          },
          {
            key: "claim",
            label: "结论级别",
            children: !hasPairedEffect
              ? "不可判定"
              : descriptiveOnly
                ? "descriptive_only"
                : asText(paired.claim, "已提供"),
          },
          {
            key: "delta",
            label: "观测差值",
            children:
              descriptiveOnly || delta === null ? "已隐藏" : delta.toFixed(4),
          },
          {
            key: "definition",
            label: "差值定义",
            children: asText(
              paired.definition,
              hasPairedEffect ? "—" : "后端未提供定义",
            ),
            span: 2,
          },
          {
            key: "warning",
            label: "统计边界",
            children: asText(
              paired.warning,
              "未提供统计警告；不得据此单独宣称泛化收益。",
            ),
            span: 2,
          },
        ]}
      />
      {hasPairedEffect ? (
        <JsonSection title="paired_effect" value={paired} />
      ) : null}
    </>
  );
}

function SummaryTab({ detail }: { detail: KnowledgeExperimentDetail }) {
  const summary = asObject(detail.summary);
  const byArm = asObject(summary.by_arm);
  const paired = asObject(summary.paired_effect ?? detail.paired_effect);
  const sampleCount =
    asNumber(paired.sample_count) ?? asNumber(paired.pairs) ?? 0;
  const minimum = asNumber(paired.minimum_recommended_samples) ?? 10;
  const descriptiveOnly =
    sampleCount < minimum ||
    paired.sample_adequate === false ||
    hasDescriptiveOnlyMarker(paired.claim) ||
    hasDescriptiveOnlyMarker(paired.warning) ||
    hasDescriptiveOnlyMarker(summary.claim) ||
    hasDescriptiveOnlyMarker(summary.warning);
  const delta =
    asNumber(paired.success_delta) ??
    asNumber(paired.on_minus_off_success_delta);

  return (
    <>
      <Alert
        type="info"
        showIcon
        message="严格成功口径"
        description="只有 evaluation_report.final_result == SUCCESS 才计为解题成功；Run 生命周期状态不算解题成功。"
      />
      {descriptiveOnly ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message="当前结果仅可描述，不能据此宣称成功率提升"
          description={`配对样本 ${sampleCount} 个，少于建议的 ${minimum} 个。页面隐藏效果性差值，避免把描述性结果误读为泛化收益。`}
        />
      ) : (
        <Alert
          type="success"
          showIcon
          style={{ marginTop: 12 }}
          message="样本量达到当前展示门槛"
          description="结果仍属于描述性 A/B 对比，不自动等同于统计显著性或题型泛化结论。"
        />
      )}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <ArmCard arm="off" value={byArm.off} />
        </Col>
        <Col xs={24} lg={12}>
          <ArmCard arm="on" value={byArm.on} />
        </Col>
      </Row>
      <Descriptions
        bordered
        size="small"
        column={{ xs: 1, sm: 1, md: 2 }}
        style={{ marginTop: 16 }}
        items={[
          {
            key: "records",
            label: "记录数",
            children: asNumber(summary.record_count) ?? detail.record_count,
          },
          {
            key: "paired-count",
            label: "配对样本",
            children: sampleCount,
          },
          {
            key: "claim",
            label: "结论级别",
            children: asText(paired.claim, "—"),
          },
          {
            key: "delta",
            label: "观测差值",
            children: descriptiveOnly
              ? "已隐藏（样本不足）"
              : delta === null
                ? "—"
                : delta.toFixed(4),
          },
          {
            key: "definition",
            label: "差值定义",
            children: asText(paired.definition),
            span: 2,
          },
          {
            key: "warning",
            label: "统计边界",
            children: asText(
              paired.warning,
              "未发现额外统计警告；仍需结合题型分层与置信区间解释。",
            ),
            span: 2,
          },
        ]}
      />
      <JsonSection title="summary.json" value={summary} />
    </>
  );
}

function ByArmPanel({
  record,
  summary,
}: {
  record: unknown;
  summary: JsonObject;
}) {
  const raw = asObject(record);
  const byArm = asObject(summary.by_arm ?? raw.by_arm);
  const entries = Object.entries(byArm);
  if (!entries.length) return null;

  const order = ["baseline", "off", "control", "treatment", "on"];
  entries.sort(([left], [right]) => {
    const leftIndex = order.indexOf(left.toLowerCase());
    const rightIndex = order.indexOf(right.toLowerCase());
    return (leftIndex === -1 ? 99 : leftIndex) -
      (rightIndex === -1 ? 99 : rightIndex);
  });

  return (
    <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
      {entries.map(([arm, value]) => (
        <Col key={arm} xs={24} lg={12}>
          <ArmCard arm={arm} value={value} />
        </Col>
      ))}
    </Row>
  );
}

function PairRunCard({ run }: { run: NormalizedRunArm }) {
  const roleLabel = run.role === "baseline" ? "Baseline" : "Treatment";
  const lifecycleStatus = run.status ?? "UNKNOWN";
  const evaluatorEvidence =
    run.finalResult === null ? "未评估/不可判定" : run.finalResult;

  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Tag color={run.role === "baseline" ? "default" : "blue"}>
            {roleLabel}
          </Tag>
          <Tag color={run.knowledgeMode === "on" ? "blue" : "default"}>
            knowledge_mode={run.knowledgeMode ?? "—"}
          </Tag>
        </Space>
      }
    >
      <Descriptions
        bordered
        size="small"
        column={1}
        items={[
          {
            key: "run-id",
            label: "Run ID",
            children: run.runId ? (
              <Link to={`/runs/${run.runId}`}>
                <Text code>{run.runId}</Text>
              </Link>
            ) : (
              "—"
            ),
          },
          {
            key: "lifecycle",
            label: "生命周期状态",
            children: (
              <Tag color={statusColor(lifecycleStatus.toUpperCase())}>
                {lifecycleStatus}
              </Tag>
            ),
          },
          {
            key: "evaluator",
            label: "Evaluator final_result",
            children:
              run.finalResult === null ? (
                <Tag color="warning">{evaluatorEvidence}</Tag>
              ) : (
                <Tag color={run.strictSuccess ? "success" : "error"}>
                  {evaluatorEvidence}
                </Tag>
              ),
          },
          {
            key: "strict-success",
            label: "严格成功",
            children:
              run.strictSuccess === null
                ? "不可判定（缺少 evaluator 证据）"
                : run.strictSuccess
                  ? "是"
                  : "否",
          },
          {
            key: "started",
            label: "开始时间",
            children: formatTime(run.startedAt),
          },
          {
            key: "completed",
            label: "完成时间",
            children: formatTime(run.completedAt),
          },
          {
            key: "error",
            label: "失败错误",
            children: run.error ? (
              <Tooltip title={run.error}>
                <Text type="danger">{run.error}</Text>
              </Tooltip>
            ) : (
              "—"
            ),
          },
        ]}
      />
    </Card>
  );
}

function DatabasePairSummary({
  detail,
}: {
  detail: KnowledgeExperimentDetail;
}) {
  const summary = asObject(detail.summary);
  const metadata = experimentArmMetadata(detail);
  const source = experimentSource(detail);
  const arms = pairedRunArms(detail);
  const baselineValue =
    metadata.baselineValue ?? arms.baseline.knowledgeMode;
  const treatmentValue =
    metadata.treatmentValue ?? arms.treatment.knowledgeMode;

  return (
    <>
      <Alert
        type="info"
        showIcon
        message="严格成功口径"
        description="只有 evaluation_report.final_result == SUCCESS 才算 evaluator-backed success；COMPLETED 或 COMPLETED_SOLVED 只是 Run 生命周期状态。"
      />
      <Descriptions
        bordered
        size="small"
        column={{ xs: 1, sm: 1, md: 2 }}
        style={{ marginTop: 16 }}
        items={[
          {
            key: "source",
            label: "来源",
            children: <Tag color={source.color}>{source.label}</Tag>,
          },
          {
            key: "factor",
            label: "实验因子",
            children: metadata.factor ?? "—",
          },
          {
            key: "baseline-mode",
            label: "Baseline 值",
            children: baselineValue ?? "—",
          },
          {
            key: "treatment-mode",
            label: "Treatment 值",
            children: treatmentValue ?? "—",
          },
          {
            key: "baseline-run",
            label: "Baseline Run",
            children: arms.baseline.runId ? (
              <Link to={`/runs/${arms.baseline.runId}`}>
                <Text code>{arms.baseline.runId}</Text>
              </Link>
            ) : (
              "—"
            ),
          },
          {
            key: "treatment-run",
            label: "Treatment Run",
            children: arms.treatment.runId ? (
              <Link to={`/runs/${arms.treatment.runId}`}>
                <Text code>{arms.treatment.runId}</Text>
              </Link>
            ) : (
              "—"
            ),
          },
        ]}
      />
      <PairedEffectPanel record={detail} summary={summary} />
      <ByArmPanel record={detail} summary={summary} />
      {hasObjectKeys(summary) ? (
        <JsonSection title="实验 summary" value={summary} />
      ) : null}
    </>
  );
}

function DatabaseRunsTab({ detail }: { detail: KnowledgeExperimentDetail }) {
  const arms = pairedRunArms(detail);

  return (
    <>
      <Alert
        type="warning"
        showIcon
        message="生命周期状态与解题成功分开判断"
        description="本视图只把 evaluation_report.final_result == SUCCESS 标为严格成功；没有评估报告时显示未评估/不可判定。"
      />
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} xl={12}>
          <PairRunCard run={arms.baseline} />
        </Col>
        <Col xs={24} xl={12}>
          <PairRunCard run={arms.treatment} />
        </Col>
      </Row>
      <JsonSection
        title="Run 原始配对数据"
        value={{
          baseline: arms.baseline.raw,
          treatment: arms.treatment.raw,
        }}
      />
    </>
  );
}

function ManifestTab({ detail }: { detail: KnowledgeExperimentDetail }) {
  const manifest = asObject(detail.manifest);
  const cases = asArray(manifest.cases).map(asObject);
  const runtime = asObject(manifest.runtime);
  const executor = asObject(runtime.executor);
  const preflight = asObject(executor.preflight);

  return (
    <>
      <Descriptions
        bordered
        size="small"
        column={{ xs: 1, sm: 1, md: 2 }}
        items={[
          { key: "schema", label: "Schema", children: asText(manifest.schema_version) },
          {
            key: "immutable",
            label: "不可变快照",
            children: manifest.immutable === true ? "是" : "否",
          },
          { key: "created", label: "创建时间", children: formatTime(manifest.created_at) },
          {
            key: "fingerprint",
            label: "配置指纹",
            children: <Text code>{asText(manifest.configuration_fingerprint)}</Text>,
            span: 2,
          },
          {
            key: "arms",
            label: "实验臂",
            children: (
              <Space wrap>
                {asArray(manifest.arms).map((arm) => (
                  <Tag key={String(arm)} color={arm === "on" ? "blue" : "default"}>
                    {String(arm).toUpperCase()}
                  </Tag>
                ))}
              </Space>
            ),
          },
          { key: "repeats", label: "重复次数", children: asText(manifest.repeats) },
          {
            key: "executor",
            label: "执行器",
            children: asText(executor.name),
          },
          {
            key: "preflight",
            label: "预检状态",
            children: (
              <Tag color={statusColor(asText(preflight.status, "UNKNOWN").toUpperCase())}>
                {asText(preflight.status)}
              </Tag>
            ),
          },
        ]}
      />
      <Card
        size="small"
        title="Case 清单"
        style={{ marginTop: 16 }}
      >
        <Table<JsonObject>
          rowKey={(row) => asText(row.case_id, JSON.stringify(row))}
          dataSource={cases}
          size="small"
          pagination={false}
          scroll={{ x: 620 }}
          columns={[
            { title: "case_id", dataIndex: "case_id", width: 220 },
            {
              title: "路径",
              dataIndex: "path",
              render: (value: unknown) => <Text code>{asText(value)}</Text>,
            },
            {
              title: "定义 SHA-256",
              dataIndex: "definition_sha256",
              width: 260,
              render: (value: unknown) => (
                <Tooltip title={asText(value)}>
                  <Text code>{asText(value).slice(0, 16)}…</Text>
                </Tooltip>
              ),
            },
          ]}
        />
      </Card>
      <JsonSection title="manifest.json" value={manifest} />
    </>
  );
}

function PlanTab({ detail }: { detail: KnowledgeExperimentDetail }) {
  const plan = asObject(detail.plan);
  const tasks = asArray(plan.tasks).map(asObject);

  return (
    <>
      <Descriptions
        bordered
        size="small"
        column={{ xs: 1, sm: 1, md: 2 }}
        items={[
          { key: "schema", label: "Schema", children: asText(plan.schema_version) },
          { key: "created", label: "创建时间", children: formatTime(plan.created_at) },
          {
            key: "dry-run",
            label: "Dry run",
            children: plan.dry_run === true ? "是（未创建 Run）" : "否",
          },
          {
            key: "resume",
            label: "Resume",
            children: plan.resume === true ? "是" : "否",
          },
          {
            key: "tasks",
            label: "计划任务",
            children: tasks.length,
          },
          {
            key: "fingerprint",
            label: "配置指纹",
            children: <Text code>{asText(plan.configuration_fingerprint)}</Text>,
          },
        ]}
      />
      <Card size="small" title="计划任务" style={{ marginTop: 16 }}>
        <Table<JsonObject>
          rowKey={(row) =>
            `${asText(row.case_id)}-${asText(row.arm)}-${asText(row.repeat)}`
          }
          dataSource={tasks}
          size="small"
          pagination={{ pageSize: 10, showSizeChanger: false }}
          scroll={{ x: 680 }}
          columns={[
            { title: "case_id", dataIndex: "case_id", width: 220 },
            {
              title: "arm",
              dataIndex: "arm",
              width: 90,
              render: (value: unknown) => (
                <Tag color={value === "on" ? "blue" : "default"}>
                  {String(value ?? "—").toUpperCase()}
                </Tag>
              ),
            },
            { title: "repeat", dataIndex: "repeat", width: 90 },
            {
              title: "artifact",
              dataIndex: "artifact",
              render: (value: unknown) => <Text code>{asText(value)}</Text>,
            },
          ]}
        />
      </Card>
      <JsonSection title="plan.json" value={plan} />
    </>
  );
}

function ArtifactsTab({
  artifacts,
}: {
  artifacts: KnowledgeExperimentDetail["artifacts"];
}) {
  const [rawRecord, setRawRecord] = useState<JsonObject | null>(null);
  const rows = artifacts.map((artifact) => {
    const artifactRecord = asObject(artifact);
    const payload = asObject(artifactRecord.data);
    return {
      ...payload,
      __artifact: artifactRecord,
      __artifact_name: artifactRecord.name,
      __artifact_omitted: artifactRecord.omitted,
      __artifact_error:
        artifactRecord.error ?? artifactRecord.reason ?? null,
    };
  });
  const columns: TableProps<JsonObject>["columns"] = [
    {
      title: "case_id",
      dataIndex: "case_id",
      width: 210,
      fixed: "left",
      render: (value: unknown) => <Text code>{asText(value)}</Text>,
    },
    {
      title: "arm",
      dataIndex: "arm",
      width: 82,
      render: (value: unknown) => (
        <Tag color={value === "on" ? "blue" : "default"}>
          {String(value ?? "—").toUpperCase()}
        </Tag>
      ),
    },
    { title: "repeat", dataIndex: "repeat", width: 78 },
    {
      title: "status",
      dataIndex: "status",
      width: 150,
      render: (value: unknown) => {
        const status = asText(value, "UNKNOWN");
        return <Tag color={statusColor(status.toUpperCase())}>{status}</Tag>;
      },
    },
    {
      title: "success",
      dataIndex: "success",
      width: 96,
      render: (value: unknown, row) => {
        if (row.__artifact_omitted === true || row.__artifact_error) {
          return <Tag color="warning">不可用</Tag>;
        }
        if (value === true) {
          return <Tag color="success">成功</Tag>;
        }
        if (value === false) {
          return <Tag color="error">未成功</Tag>;
        }
        return (
          <Tooltip title="该 artifact 没有 evaluator-backed success 字段">
            <Tag>—</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "duration",
      dataIndex: "duration_seconds",
      width: 120,
      render: formatDuration,
    },
    { title: "tool_calls", dataIndex: "tool_calls", width: 100 },
    {
      title: "run_id",
      dataIndex: "run_id",
      width: 160,
      render: (value: unknown) => {
        const runId = asText(value, "");
        if (!runId) return "—";
        return (
          <Link to={`/runs/${runId}`}>
            <Text code>{runId.slice(0, 12)}</Text>
          </Link>
        );
      },
    },
    {
      title: "error",
      dataIndex: "error",
      width: 220,
      ellipsis: true,
      render: (value: unknown, row) => {
        const message =
          asText(value, "") || asText(row.__artifact_error, "");
        return message ? (
          <Tooltip title={message}>
            <Text type="danger">{message}</Text>
          </Tooltip>
        ) : (
          "—"
        );
      },
    },
    {
      title: "原始 JSON",
      key: "raw",
      fixed: "right",
      width: 112,
      render: (_, row) => (
        <Button
          type="link"
          size="small"
          icon={<CodeOutlined />}
          onClick={() => setRawRecord(asObject(row.__artifact))}
        >
          查看
        </Button>
      ),
    },
  ];

  return (
    <>
      <Alert
        type="info"
        showIcon
        message="Artifact 成功字段来自 evaluator"
        description="不要把 status=COMPLETED 或 Run 未报错直接视为解题成功。"
      />
      <Card size="small" title="逐次执行记录" style={{ marginTop: 16 }}>
        <Table<JsonObject>
          rowKey={(row) =>
            asText(
              row.__artifact_name,
              `${asText(row.case_id)}-${asText(row.arm)}-${asText(row.repeat)}`,
            )
          }
          dataSource={rows}
          columns={columns}
          size="small"
          pagination={{ pageSize: 10, showSizeChanger: false }}
          scroll={{ x: 1380 }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="尚无执行 artifact"
              />
            ),
          }}
        />
      </Card>
      <Modal
        open={rawRecord !== null}
        title={
          rawRecord
            ? `${asText(rawRecord.case_id)} / ${asText(rawRecord.arm)} / repeat ${asText(rawRecord.repeat)}`
            : "原始 Artifact"
        }
        footer={<Button onClick={() => setRawRecord(null)}>关闭</Button>}
        onCancel={() => setRawRecord(null)}
        width={860}
        destroyOnClose
      >
        <JsonBlock value={rawRecord} />
      </Modal>
    </>
  );
}

function ExperimentDetailPanel({
  detail,
  loading,
  error,
}: {
  detail?: KnowledgeExperimentDetail;
  loading: boolean;
  error: Error | null;
}) {
  if (error) {
    return (
      <Card className="panel-card">
        <Alert
          type="error"
          showIcon
          message="实验详情加载失败"
          description={error.message}
        />
      </Card>
    );
  }
  if (!detail) {
    return (
      <Card className="panel-card" loading={loading}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="选择一个实验查看审计详情"
        />
      </Card>
    );
  }
  const source = experimentSource(detail);
  const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
  const isDatabasePair = source.kind === "db_pair";
  const tabs = isDatabasePair
    ? [
        {
          key: "pair-summary",
          label: "配对摘要",
          children: <DatabasePairSummary detail={detail} />,
        },
        {
          key: "runs",
          label: "Run 对比",
          children: <DatabaseRunsTab detail={detail} />,
        },
        {
          key: "raw",
          label: "原始 JSON",
          children: (
            <JsonSection title="DB 配对实验原始 JSON" value={detail} />
          ),
        },
      ]
    : [
        {
          key: "summary",
          label: "摘要",
          children: <SummaryTab detail={detail} />,
        },
        {
          key: "manifest",
          label: "Manifest",
          children: <ManifestTab detail={detail} />,
        },
        {
          key: "plan",
          label: "Plan",
          children: <PlanTab detail={detail} />,
        },
        {
          key: "artifacts",
          label: `Artifacts (${artifacts.length})`,
          children: <ArtifactsTab artifacts={artifacts} />,
        },
        {
          key: "raw",
          label: "原始 JSON",
          children: (
            <JsonSection
              title={
                hasObjectKeys(detail.manifest) ||
                hasObjectKeys(detail.plan) ||
                artifacts.length
                  ? "文件实验原始 JSON"
                  : "实验原始 JSON fallback"
              }
              value={detail}
            />
          ),
        },
      ];

  return (
    <Card className="panel-card" loading={loading}>
      <Space direction="vertical" size={6}>
        <Space wrap>
          <ExperimentSourceTag record={detail} />
          <Text type="secondary">实验 ID</Text>
        </Space>
        <Title level={4} style={{ margin: 0 }}>
          <Text code>{asText(detail.id, "未知实验")}</Text>
        </Title>
      </Space>
      <Tabs style={{ marginTop: 18 }} items={tabs} />
    </Card>
  );
}

export function KnowledgeExperimentsPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const listQuery = useQuery({
    queryKey: ["knowledge-experiments"],
    queryFn: api.listKnowledgeExperiments,
  });
  const experiments = listQuery.data ?? [];

  useEffect(() => {
    if (!experiments.length) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !experiments.some((item) => item.id === selectedId)) {
      setSelectedId(experiments[0].id);
    }
  }, [experiments, selectedId]);

  const detailQuery = useQuery({
    queryKey: ["knowledge-experiment", selectedId],
    queryFn: () => api.getKnowledgeExperiment(selectedId as string),
    enabled: Boolean(selectedId),
  });

  const listColumns: TableProps<KnowledgeExperimentSummary>["columns"] = [
    {
      title: "实验",
      dataIndex: "id",
      width: 190,
      render: (value: string) => (
        <Space direction="vertical" size={0}>
          <Text strong>{value.slice(0, 18)}</Text>
          {value.length > 18 ? <Text code>{value}</Text> : null}
        </Space>
      ),
    },
    {
      title: "来源",
      key: "source",
      width: 102,
      render: (_, record) => <ExperimentSourceTag record={record} />,
    },
    {
      title: "知识模式 / 配对 Run",
      key: "pairing",
      width: 230,
      render: (_, record) => {
        const metadata = experimentArmMetadata(record);
        const runs = pairedRunArms(record);
        const baselineValue =
          metadata.baselineValue ?? runs.baseline.knowledgeMode;
        const treatmentValue =
          metadata.treatmentValue ?? runs.treatment.knowledgeMode;
        const baselineRunId =
          metadata.baselineRunId ?? runs.baseline.runId;
        const treatmentRunId =
          metadata.treatmentRunId ?? runs.treatment.runId;
        const hasPairingMetadata = [
          metadata.factor,
          baselineValue,
          treatmentValue,
          baselineRunId,
          treatmentRunId,
        ].some(Boolean);
        if (!hasPairingMetadata) return <Text type="secondary">—</Text>;

        return (
          <Space direction="vertical" size={3}>
            {metadata.factor ? (
              <Text type="secondary">{metadata.factor}</Text>
            ) : null}
            <Space wrap size={[4, 4]}>
              {baselineValue ? (
                <Tag>baseline={baselineValue}</Tag>
              ) : null}
              {treatmentValue ? (
                <Tag color="blue">treatment={treatmentValue}</Tag>
              ) : null}
            </Space>
            {baselineRunId || treatmentRunId ? (
              <Space wrap size={[8, 2]}>
                {baselineRunId ? (
                  <Link to={`/runs/${baselineRunId}`}>
                    <Text code>{baselineRunId.slice(0, 10)}</Text>
                  </Link>
                ) : null}
                {treatmentRunId ? (
                  <Link to={`/runs/${treatmentRunId}`}>
                    <Text code>{treatmentRunId.slice(0, 10)}</Text>
                  </Link>
                ) : null}
              </Space>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: unknown) => {
        const status = asText(value, "UNKNOWN");
        return <Tag color={statusColor(status.toUpperCase())}>{status}</Tag>;
      },
    },
    {
      title: "Cases",
      dataIndex: "case_count",
      width: 82,
      align: "right",
    },
    {
      title: "计划任务",
      dataIndex: "planned_jobs",
      width: 96,
      align: "right",
    },
    {
      title: "记录",
      dataIndex: "record_count",
      width: 76,
      align: "right",
    },
    {
      title: "配对效应",
      dataIndex: "paired_effect",
      width: 150,
      render: (_, record) => {
        const paired = pairedEffect(record);
        if (!Object.keys(paired).length) {
          return <Text type="secondary">未提供</Text>;
        }
        const count = asNumber(paired.sample_count) ?? asNumber(paired.pairs) ?? 0;
        const minimum =
          asNumber(paired.minimum_recommended_samples) ?? 10;
        const descriptiveOnly =
          count < minimum ||
          paired.sample_adequate === false ||
          hasDescriptiveOnlyMarker(paired.claim) ||
          hasDescriptiveOnlyMarker(paired.warning);
        const claim = descriptiveOnly
          ? "descriptive_only"
          : asText(paired.claim, "—");
        return (
          <Space direction="vertical" size={2}>
            <Tag color={descriptiveOnly ? "warning" : "cyan"}>
              {claim}
            </Tag>
            <Text type="secondary">n={count}</Text>
          </Space>
        );
      },
    },
    {
      title: "时间",
      width: 178,
      render: (_, record) => experimentTime(record),
    },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>A/B 实验审计</h1>
          <p>核对交互式配对 Run、文件型实验产物和严格成功口径。</p>
        </div>
        <Button
          icon={<ReloadOutlined />}
          loading={listQuery.isFetching}
          onClick={() => void listQuery.refetch()}
        >
          刷新
        </Button>
      </div>
      {listQuery.error ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="实验列表加载失败"
          description={(listQuery.error as Error).message}
        />
      ) : null}
      <Row gutter={[16, 16]} align="top">
        <Col xs={24} xl={9} xxl={8}>
          <Card
            className="panel-card"
            title={
              <Space>
                <ExperimentOutlined />
                <span>实验列表</span>
              </Space>
            }
            extra={<Text type="secondary">{experiments.length} 个</Text>}
          >
            <Table<KnowledgeExperimentSummary>
              className="cyber-table"
              rowKey="id"
              dataSource={experiments}
              columns={listColumns}
              loading={listQuery.isLoading}
              pagination={
                experiments.length > 8
                  ? { pageSize: 8, showSizeChanger: false }
                  : false
              }
              scroll={{ x: 1260 }}
              rowClassName={(record) =>
                record.id === selectedId ? "ant-table-row-selected" : ""
              }
              onRow={(record) => ({
                onClick: () => setSelectedId(record.id),
              })}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={
                      <Space direction="vertical" size={6}>
                        <span>尚无知识增强实验</span>
                        <Paragraph
                          type="secondary"
                          style={{ margin: 0, fontSize: 12 }}
                          copyable={{
                            text: "python scripts/run_knowledge_experiment.py --output-dir data/knowledge_experiments/<experiment-id> --dry-run",
                          }}
                        >
                          可从 Run 页面创建配对实验，或用 CLI dry-run 生成 manifest/plan
                        </Paragraph>
                      </Space>
                    }
                  />
                ),
              }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={15} xxl={16}>
          <ExperimentDetailPanel
            detail={detailQuery.data}
            loading={detailQuery.isLoading || detailQuery.isFetching}
            error={detailQuery.error instanceof Error ? detailQuery.error : null}
          />
        </Col>
      </Row>
      {!experiments.length && !listQuery.isLoading ? (
        <Card size="small" style={{ marginTop: 16 }}>
          <Space>
            <FileSearchOutlined />
            <Text type="secondary">
              前端只读审计；交互式配对实验与 CLI 文件实验都会汇总到这里。
            </Text>
            <Text code>
              python scripts/run_knowledge_experiment.py --output-dir
              data/knowledge_experiments/run-001 --resume
            </Text>
          </Space>
        </Card>
      ) : null}
    </>
  );
}
