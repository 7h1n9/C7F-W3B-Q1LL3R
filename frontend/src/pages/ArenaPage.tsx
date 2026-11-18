import {
  ApiOutlined,
  BookOutlined,
  CloudDownloadOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DeploymentUnitOutlined,
  ExperimentOutlined,
  KeyOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RocketOutlined,
  SearchOutlined,
  SecurityScanOutlined,
  StarOutlined,
  SyncOutlined,
  ThunderboltOutlined,
  TrophyOutlined,
  UnorderedListOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Pagination,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../services/api";
import type {
  ArenaBucket,
  ArenaChallenge,
  ArenaCheck,
  ArenaConnection,
  ArenaConnectionInput,
  ArenaGround,
  ArenaMcpChallenge,
  ArenaQueueBulkAction,
  ArenaQueueItem,
  ArenaQueueScheduler,
  ArenaSolveTask,
  ArenaSubmission,
  ArenaWorkerEngineInput,
} from "../types/api";

// The platform's open API root. Prefilled because it is the same for every
// operator; a self-hosted or staging arena stays a one-field edit.
const DEFAULT_BASE_URL = "https://ctf2.dasctf.com/api/open/v1/user";

const CATALOGUE_PAGE_SIZE = 100;

type ConnectionForm = {
  name: string;
  base_url: string;
  token?: string;
  enabled: boolean;
  requests_per_minute: number;
  max_concurrency: number;
  default_model_config_id?: string;
  default_engine_type?: string;
  default_solver_mode?: string;
  default_reason_model_config_id?: string;
  // Muteki's multi-engine split. Each row is one worker engine plus its model;
  // an empty list is the single-model case above, which is why this is optional.
  default_worker_engines?: ArenaWorkerEngineInput[];
};

// The platform's catalogue labels queue statuses; a colour per status keeps the
// table scannable, and the labels are Chinese because the rest of the page is.
const QUEUE_LABEL: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  SOLVED: "已解出",
  UNSOLVED: "未解出",
  FAILED: "失败",
  CANCELLED: "已取消",
};
const QUEUE_TAG_COLOR: Record<string, string> = {
  PENDING: "default",
  RUNNING: "processing",
  SOLVED: "success",
  UNSOLVED: "warning",
  FAILED: "error",
  CANCELLED: "default",
};

function CheckTag({ ok }: { ok: boolean }) {
  return <Tag color={ok ? "success" : "error"}>{ok ? "正常" : "异常"}</Tag>;
}

// The backend reports the held environment as a check whose message names it, so
// the slot summary reads it from there rather than duplicating lease state in a
// second endpoint that could disagree.
function arenaLocalLease(checks?: ArenaCheck[]): string | null {
  const slot = (checks ?? []).find((check) => check.name === "environment_slot");
  if (!slot || slot.message.startsWith("Idle")) return null;
  return slot.message;
}

function VerdictTag({ verdict }: { verdict: string }) {
  const color =
    verdict === "accepted" ? "success" : verdict === "rejected" ? "error" : "default";
  const label =
    verdict === "accepted"
      ? "通过"
      : verdict === "rejected"
        ? "拒绝"
        : verdict === "duplicate"
          ? "重复（已拦截）"
          : verdict === "circuit_open"
            ? "熔断"
            : verdict === "already_solved"
              ? "平台已解"
              : verdict;
  return <Tag color={color}>{label}</Tag>;
}

// A rate of null means "nothing attempted", not "nothing solved". Rendering the
// two identically would turn an empty bucket into an apparent total failure.
function RateCell({ bucket }: { bucket: ArenaBucket }) {
  if (bucket.accept_rate === null || bucket.accept_rate === undefined) {
    return <span style={{ color: "#8c8c8c" }}>—</span>;
  }
  return <span>{(bucket.accept_rate * 100).toFixed(1)}%</span>;
}

const bucketColumns = [
  { title: "提交", dataIndex: "attempted", width: 70 },
  { title: "通过", dataIndex: "accepted", width: 70 },
  { title: "拒绝", dataIndex: "rejected", width: 70 },
  { title: "通过率", width: 90, render: (_: unknown, bucket: ArenaBucket) => <RateCell bucket={bucket} /> },
  {
    title: "拦截",
    width: 110,
    render: (_: unknown, bucket: ArenaBucket) =>
      bucket.duplicate_suppressed || bucket.circuit_open
        ? <Space size={4}><Tag>{bucket.duplicate_suppressed} 重复</Tag><Tag color="warning">{bucket.circuit_open} 熔断</Tag></Space>
        : <span style={{ color: "#8c8c8c" }}>—</span>,
  },
  {
    title: "Token",
    width: 90,
    render: (_: unknown, bucket: ArenaBucket) => bucket.token_cost ?? "—",
  },
];

function bucketRows(source: Record<string, ArenaBucket>) {
  return Object.entries(source).map(([key, value]) => ({ key, ...value }));
}

// Text the operator needs to take elsewhere (a flag, an id, a URL). Copying is
// the whole point of showing it, so it is one click rather than a selection.
function CopyText({ text, children }: { text: string; children?: ReactNode }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      message.success("已复制");
    } catch {
      message.warning("浏览器拒绝了剪贴板访问，请手动复制");
    }
  };
  return (
    <Space size={6}>
      {children ?? <Typography.Text code>{text}</Typography.Text>}
      <Tooltip title="复制">
        <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => void copy()} />
      </Tooltip>
    </Space>
  );
}

// A watchdog ceiling that has not been ticked within is the signature of a
// wedged loop, so the scheduler block leads with it rather than with "running".
function SchedulerTag({ scheduler }: { scheduler?: ArenaQueueScheduler }) {
  if (!scheduler) return <Tag>调度器状态未知</Tag>;
  if (!scheduler.running) return <Tag>调度未启动</Tag>;
  // An item in flight means the loop is *inside* the solve and legitimately not
  // ticking, so the tick age is not evidence of a stall. Only the item's own age
  // is, and only as it approaches the watchdog.
  const itemAge = scheduler.current_item_age_seconds;
  if (itemAge !== null) {
    const nearWatchdog = itemAge > scheduler.watchdog_seconds;
    return (
      <Tag color={nearWatchdog ? "error" : "processing"} icon={nearWatchdog ? <WarningOutlined /> : <SyncOutlined spin />}>
        正在跑 1 题（已 {Math.round(itemAge)}s / 上限 {Math.round(scheduler.watchdog_seconds)}s）
      </Tag>
    );
  }
  const age = scheduler.last_tick_age_seconds;
  if (age !== null && age > scheduler.watchdog_seconds) {
    return (
      <Tag color="error" icon={<WarningOutlined />}>
        疑似卡死（{Math.round(age)}s 无心跳且无在飞任务）
      </Tag>
    );
  }
  return (
    <Tag color="processing" icon={<SyncOutlined spin />}>
      调度中（等待取题）{age !== null ? ` · 心跳 ${Math.round(age)}s` : ""}
    </Tag>
  );
}

export function ArenaPage() {
  const client = useQueryClient();
  const [form] = Form.useForm<ConnectionForm>();
  const [tab, setTab] = useState("catalogue");

  // -- connection maintenance ------------------------------------------------
  const [editing, setEditing] = useState<ArenaConnection>();
  const [open, setOpen] = useState(false);
  const [removing, setRemoving] = useState<ArenaConnection>();

  // -- catalogue -------------------------------------------------------------
  const [groundId, setGroundId] = useState<string>();
  const [cataloguePage, setCataloguePage] = useState(1);
  const [catalogueCategory, setCatalogueCategory] = useState<string>();
  const [catalogueDifficulty, setCatalogueDifficulty] = useState<string>();
  const [catalogueUnsolvedOnly, setCatalogueUnsolvedOnly] = useState(false);
  const [catalogueSearch, setCatalogueSearch] = useState("");
  const [selectedChallenges, setSelectedChallenges] = useState<string[]>([]);
  const [detail, setDetail] = useState<ArenaMcpChallenge>();
  // Facets accumulate rather than being derived from the current page: a derived
  // list would collapse to the single value already selected, and the operator
  // could not switch back without reloading the page.
  const [facets, setFacets] = useState<{ categories: string[]; difficulties: string[] }>({
    categories: [],
    difficulties: [],
  });

  // -- queue -----------------------------------------------------------------
  const [queueStatus, setQueueStatus] = useState<string>();
  const [queueSearch, setQueueSearch] = useState("");
  const [selectedQueue, setSelectedQueue] = useState<string[]>([]);
  const [clearStatus, setClearStatus] = useState("PENDING");

  // -- solve -----------------------------------------------------------------
  const [solveTaskId, setSolveTaskId] = useState<string>();
  const [reclaimOpen, setReclaimOpen] = useState(false);
  const [reclaimChallengeId, setReclaimChallengeId] = useState<string>();
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteIds, setPasteIds] = useState("");
  const [enqueueOrder, setEnqueueOrder] = useState<"offline_first" | "container_first" | "as_listed">("container_first");
  const [enqueueLimit, setEnqueueLimit] = useState(10);

  const connections = useQuery({ queryKey: ["arena-connections"], queryFn: api.listArenaConnections });
  const status = useQuery({ queryKey: ["arena-status"], queryFn: api.getArenaStatus, refetchInterval: 30_000 });
  const submissions = useQuery({ queryKey: ["arena-submissions"], queryFn: () => api.listArenaSubmissions() });
  const models = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });
  // The catalogue needs a connection id, so it stays idle until one is enabled.
  const activeConnection = connections.data?.find((item) => item.enabled);

  const grounds = useQuery({
    queryKey: ["arena-grounds", activeConnection?.id],
    queryFn: () => api.listArenaGrounds(activeConnection!.id),
    enabled: Boolean(activeConnection),
  });
  // The MCP catalogue is the path a solve actually uses: it carries the container
  // flag and the paging the REST surface does not have.
  const mcpChallenges = useQuery({
    queryKey: [
      "arena-mcp-challenges",
      activeConnection?.id,
      groundId,
      catalogueCategory,
      catalogueDifficulty,
      catalogueUnsolvedOnly,
      cataloguePage,
    ],
    queryFn: () =>
      api.listArenaMcpChallenges(activeConnection!.id, groundId!, {
        category: catalogueCategory,
        difficulty: catalogueDifficulty,
        unsolvedOnly: catalogueUnsolvedOnly,
        page: cataloguePage,
        limit: CATALOGUE_PAGE_SIZE,
      }),
    enabled: Boolean(activeConnection && groundId),
    placeholderData: (previous) => previous,
  });
  const orphans = useQuery({
    queryKey: ["arena-orphans", activeConnection?.id],
    queryFn: () => api.getArenaOrphans(activeConnection!.id),
    enabled: Boolean(activeConnection),
  });
  // Poll only while a solve is in flight; a finished task never changes.
  const solveTask = useQuery({
    queryKey: ["arena-solve-task", solveTaskId],
    queryFn: () => api.getArenaSolveTask(solveTaskId!),
    enabled: Boolean(solveTaskId),
    refetchInterval: (query) => ((query.state.data as ArenaSolveTask | undefined)?.state === "running" ? 5000 : false),
  });

  // Queue polling is conditional: a running batch changes every few seconds, an
  // idle one never does, and polling a stopped scheduler would be pure load.
  const queue = useQuery({
    queryKey: ["arena-queue"],
    queryFn: api.getArenaQueue,
    refetchInterval: (query) => ((query.state.data as { scheduler?: ArenaQueueScheduler } | undefined)?.scheduler?.running ? 5000 : 15000),
  });

  const groundOptions = useMemo(
    () =>
      (grounds.data?.items ?? []).map((item: ArenaGround) => ({
        value: item.id,
        label: `${item.name ?? item.id}（${item.challenge_count ?? "?"} 题）`,
      })),
    [grounds.data],
  );

  useEffect(() => {
    const items = mcpChallenges.data?.items ?? [];
    if (!items.length) return;
    setFacets((previous) => {
      const categories = new Set(previous.categories);
      const difficulties = new Set(previous.difficulties);
      items.forEach((row) => {
        if (row.category) categories.add(String(row.category));
        if (row.difficulty) difficulties.add(String(row.difficulty));
      });
      return { categories: [...categories].sort(), difficulties: [...difficulties].sort() };
    });
  }, [mcpChallenges.data]);

  // A ground change invalidates the page number and the facets with it.
  useEffect(() => {
    setCataloguePage(1);
    setSelectedChallenges([]);
  }, [groundId, catalogueCategory, catalogueDifficulty, catalogueUnsolvedOnly]);

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["arena-connections"] });
    void client.invalidateQueries({ queryKey: ["arena-status"] });
    void client.invalidateQueries({ queryKey: ["arena-submissions"] });
    void client.invalidateQueries({ queryKey: ["arena-orphans"] });
  };
  const afterQueueChange = (text: string) => {
    message.success(text);
    void client.invalidateQueries({ queryKey: ["arena-queue"] });
    void client.invalidateQueries({ queryKey: ["arena-status"] });
  };

  const save = useMutation({
    mutationFn: (values: ConnectionForm) => {
      const payload: ArenaConnectionInput = {
        name: values.name,
        base_url: values.base_url,
        token: values.token || undefined,
        enabled: values.enabled,
        requests_per_minute: values.requests_per_minute,
        max_concurrency: values.max_concurrency,
        default_model_config_id: values.default_model_config_id || null,
        default_engine_type: values.default_engine_type || null,
        default_solver_mode: values.default_solver_mode || "muteki",
        default_reason_model_config_id: values.default_reason_model_config_id || null,
        // Drop rows with no engine chosen rather than sending them: the backend
        // rejects a worker without a model, and an empty row is a half-filled
        // form, not an intent to configure a worker.
        default_worker_engines: (values.default_worker_engines ?? []).filter(
          (entry) => entry?.engine_type,
        ),
      };
      return editing ? api.updateArenaConnection(editing.id, payload) : api.createArenaConnection(payload);
    },
    onSuccess: () => {
      message.success("竞技场连接已保存");
      setOpen(false);
      setEditing(undefined);
      form.resetFields();
      refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const toggleConnection = useMutation({
    mutationFn: (item: ArenaConnection) =>
      api.updateArenaConnection(item.id, {
        name: item.name,
        base_url: item.base_url,
        enabled: !item.enabled,
        requests_per_minute: item.requests_per_minute,
        max_concurrency: item.max_concurrency,
        default_model_config_id: item.default_model_config_id ?? null,
        default_engine_type: item.default_engine_type ?? null,
        default_solver_mode: item.default_solver_mode,
        default_reason_model_config_id: item.default_reason_model_config_id ?? null,
        default_worker_engines: item.default_worker_engines ?? [],
      }),
    onSuccess: (item) => {
      message.success(item.enabled ? "已启用该连接" : "已停用该连接");
      refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const probe = useMutation({
    mutationFn: api.probeArenaConnection,
    onSuccess: (result) => {
      const failed = result.checks.find((check) => !check.ok);
      failed ? message.error(failed.message) : message.success("连接正常，Token 有效");
      refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteArenaConnection(id),
    onSuccess: () => {
      message.success("竞技场连接已删除");
      setRemoving(undefined);
      refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const solve = useMutation({
    mutationFn: ({ ground, challenge }: { ground: string; challenge: string }) =>
      api.startArenaSolve(ground, challenge),
    onSuccess: (task) => {
      setSolveTaskId(task.task_id);
      message.success("已开始解题；靶机与任务在后台运行，结果会显示在下方。");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const enqueue = useMutation({
    mutationFn: api.enqueueArenaQueue,
    onSuccess: (result) => {
      const truncated = result.scan_truncated ? "（目录扫描已达页数上限）" : "";
      afterQueueChange(`已入队 ${result.added} 题（跳过重复 ${result.skipped} 题）${truncated}`);
      setSelectedChallenges([]);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const enqueueIds = useMutation({
    mutationFn: api.enqueueArenaQueueByIds,
    onSuccess: (result) => {
      const unreadable = result.unreadable_ids.length
        ? `；${result.unreadable_ids.length} 个 ID 平台读不到：${result.unreadable_ids.slice(0, 5).join(", ")}`
        : "";
      afterQueueChange(`已入队 ${result.added} 题（跳过 ${result.skipped} 题）${unreadable}`);
      setSelectedChallenges([]);
      setPasteOpen(false);
      setPasteIds("");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const startQueue = useMutation({
    mutationFn: api.startArenaQueue,
    onSuccess: () => afterQueueChange("调度已启动"),
    onError: (error: Error) => message.error(error.message),
  });
  const stopQueue = useMutation({
    mutationFn: api.stopArenaQueue,
    onSuccess: () => afterQueueChange("将在当前题完成后停止"),
    onError: (error: Error) => message.error(error.message),
  });
  const recoverQueue = useMutation({
    mutationFn: api.recoverArenaQueue,
    onSuccess: (result) =>
      afterQueueChange(
        result.repaired ? `已把 ${result.repaired} 条滞留任务退回待执行` : "没有发现滞留任务",
      ),
    onError: (error: Error) => message.error(error.message),
  });
  const cancelItem = useMutation({
    mutationFn: api.cancelArenaQueueItem,
    onSuccess: () => afterQueueChange("已取消该题"),
    onError: (error: Error) => message.error(error.message),
  });
  const prioritizeItem = useMutation({
    mutationFn: ({ id, position }: { id: string; position: "top" | "bottom" }) =>
      api.prioritizeArenaQueueItem(id, position),
    onSuccess: () => afterQueueChange("已调整执行顺序"),
    onError: (error: Error) => message.error(error.message),
  });
  const bulkQueue = useMutation({
    mutationFn: (payload: { item_ids: string[]; action: ArenaQueueBulkAction }) => api.bulkArenaQueue(payload),
    onSuccess: (result) => {
      const skipped = result.skipped.length ? `，${result.skipped.length} 条不适用` : "";
      const missing = result.missing.length ? `，${result.missing.length} 条已不存在` : "";
      afterQueueChange(`已处理 ${result.changed} 条${skipped}${missing}`);
      setSelectedQueue([]);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const clearQueue = useMutation({
    mutationFn: () => api.clearArenaQueue(clearStatus),
    onSuccess: (result) => afterQueueChange(`已清空 ${result.deleted} 条 ${QUEUE_LABEL[clearStatus] ?? clearStatus}`),
    onError: (error: Error) => message.error(error.message),
  });
  const reclaim = useMutation({
    mutationFn: (challengeId?: string) =>
      api.reclaimArenaOrphans(activeConnection!.id, { practiceGroundId: groundId, challengeId }),
    onSuccess: (result) => {
      if (result.reclaimed) {
        message.success(
          result.orphans?.orphan_suspected
            ? "已发出销毁请求，但平台仍报告有环境占用，可再试一次。"
            : "已回收泄漏的靶机，环境槽位已释放。",
        );
      } else if (result.reason === "no_lease_record") {
        message.info("没有本地租约记录；若平台仍显示占用，请手动填写题目后回收。");
      } else {
        message.error(result.error ?? `回收未成功：${result.reason}`);
      }
      setReclaimOpen(false);
      setReclaimChallengeId(undefined);
      refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const edit = (item?: ArenaConnection) => {
    setEditing(item);
    form.resetFields();
    form.setFieldsValue(
      item
        ? {
            name: item.name,
            base_url: item.base_url,
            token: undefined,
            enabled: item.enabled,
            requests_per_minute: item.requests_per_minute,
            max_concurrency: item.max_concurrency,
            default_model_config_id: item.default_model_config_id ?? undefined,
            default_engine_type: item.default_engine_type ?? undefined,
            default_solver_mode: item.default_solver_mode ?? "muteki",
            default_reason_model_config_id: item.default_reason_model_config_id ?? undefined,
            default_worker_engines: item.default_worker_engines ?? [],
          }
        : {
            base_url: DEFAULT_BASE_URL,
            enabled: true,
            requests_per_minute: 60,
            max_concurrency: 1,
            default_solver_mode: "muteki",
            default_worker_engines: [],
          },
    );
    setOpen(true);
  };

  const stats = status.data?.stats;
  const scheduler = status.data?.scheduler ?? queue.data?.scheduler;
  const counts = queue.data?.counts ?? {};
  const heldLease = arenaLocalLease(status.data?.checks);
  const catalogueItems = mcpChallenges.data?.items ?? [];
  const filteredCatalogue = catalogueItems.filter((row) => {
    if (!catalogueSearch.trim()) return true;
    const needle = catalogueSearch.trim().toLowerCase();
    return [row.name, row.friendly_id, row.category, row.id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle));
  });
  const queueItems = (queue.data?.items ?? []).filter((row) => {
    if (queueStatus && row.status !== queueStatus) return false;
    if (!queueSearch.trim()) return true;
    const needle = queueSearch.trim().toLowerCase();
    return [row.challenge_name, row.category, row.difficulty, row.platform_challenge_id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle));
  });
  const solveBusy = Boolean(solveTaskId && solveTask.data?.state === "running");
  const canStartSolve = Boolean(activeConnection?.default_model_config_id) && !solveBusy;
  // Resolved to a number up front: the platform omits `total_page`, and comparing
  // a nullable inside JSX does not narrow the way the arithmetic needs.
  const catalogueTotalPages = mcpChallenges.data?.total_page ?? 0;

  const connectionColumns: ColumnsType<ArenaConnection> = [
    {
      title: "名称",
      dataIndex: "name",
      width: 190,
      render: (value: string, item: ArenaConnection) => (
        <Space size={6}>
          <span>{value}</span>
          {!item.enabled ? <Tag>已禁用</Tag> : <Tag color="success">启用中</Tag>}
        </Space>
      ),
    },
    { title: "Base URL", dataIndex: "base_url", ellipsis: true },
    {
      title: "Token",
      dataIndex: "token_configured",
      width: 100,
      render: (value: boolean) => (value ? <Tag color="success">已配置</Tag> : <Tag color="error">未配置</Tag>),
    },
    {
      title: "上次探测",
      width: 200,
      render: (_: unknown, item: ArenaConnection) =>
        item.last_probe_at ? (
          <Space size={6}>
            <CheckTag ok={Boolean(item.last_probe_ok)} />
            <span style={{ color: "#8c8c8c", fontSize: 12 }}>
              {new Date(item.last_probe_at).toLocaleString()}
            </span>
          </Space>
        ) : (
          <span style={{ color: "#8c8c8c" }}>从未探测</span>
        ),
    },
    {
      title: "默认模型",
      width: 170,
      ellipsis: true,
      render: (_: unknown, item: ArenaConnection) => {
        const model = models.data?.find((entry) => entry.id === item.default_model_config_id);
        return model ? model.name : <span style={{ color: "#c0392b" }}>未设置（跑题会被拒绝）</span>;
      },
    },
    {
      title: "限流 / 并发",
      width: 130,
      render: (_: unknown, item: ArenaConnection) => `${item.requests_per_minute} rpm / ${item.max_concurrency}`,
    },
    {
      title: "启停",
      width: 80,
      render: (_: unknown, item: ArenaConnection) => (
        <Switch
          size="small"
          checked={item.enabled}
          loading={toggleConnection.isPending && toggleConnection.variables?.id === item.id}
          onChange={() => toggleConnection.mutate(item)}
        />
      ),
    },
    {
      title: "操作",
      width: 210,
      fixed: "right" as const,
      render: (_: unknown, item: ArenaConnection) => (
        <Space size={0} wrap>
          <Button type="link" onClick={() => edit(item)}>编辑</Button>
          <Button
            type="link"
            loading={probe.isPending && probe.variables === item.id}
            disabled={probe.isPending}
            onClick={() => probe.mutate(item.id)}
          >
            探测连接
          </Button>
          <Button type="link" danger onClick={() => setRemoving(item)}>删除</Button>
        </Space>
      ),
    },
  ];

  const submissionColumns: ColumnsType<ArenaSubmission> = [
    { title: "题目", dataIndex: "challenge_name", ellipsis: true, render: (value: string, item: ArenaSubmission) => value || item.platform_challenge_id },
    { title: "题型", dataIndex: "category", width: 100, render: (value: string) => value || "—" },
    { title: "判定", dataIndex: "verdict", width: 120, render: (value: string) => <VerdictTag verdict={value} /> },
    { title: "归因模型", dataIndex: "model_config_id", width: 160, ellipsis: true, render: (value: string | null) => value || <span style={{ color: "#8c8c8c" }}>未归因</span> },
    { title: "引擎", dataIndex: "engine_key", width: 120, render: (value: string | null) => value || <span style={{ color: "#8c8c8c" }}>—</span> },
    { title: "Token", dataIndex: "token_cost", width: 90, render: (value: number | null) => value ?? "—" },
    { title: "时间", dataIndex: "submitted_at", width: 180, render: (value: string) => new Date(value).toLocaleString() },
  ];

  const catalogueColumns: ColumnsType<ArenaMcpChallenge> = [
    {
      title: "题号",
      dataIndex: "friendly_id",
      width: 170,
      ellipsis: true,
      render: (value: string, row: ArenaMcpChallenge) => (
        <Space size={4}>
          <span className="id-code">{value || row.id.slice(0, 8)}</span>
          {row.is_solved ? <Tag color="success">已解</Tag> : null}
        </Space>
      ),
    },
    { title: "题目", dataIndex: "name", ellipsis: true, render: (value: string) => value || "—" },
    { title: "题型", dataIndex: "category", width: 110, render: (value: string) => value ? <Tag>{value}</Tag> : "—" },
    { title: "难度", dataIndex: "difficulty", width: 90, render: (value: string) => value || "—" },
    {
      title: "环境",
      width: 110,
      render: (_: unknown, row: ArenaMcpChallenge) => (
        <Space size={4}>
          {row.has_container ? <Tag color="processing">靶机</Tag> : <Tag>离线</Tag>}
          {row.has_attachments ? <Tag color="purple">附件</Tag> : null}
        </Space>
      ),
    },
    { title: "解出数", dataIndex: "solve_count", width: 80, render: (value: number) => value ?? "—" },
    {
      title: "操作",
      width: 190,
      fixed: "right" as const,
      render: (_: unknown, row: ArenaMcpChallenge) => (
        <Space size={0} wrap>
          <Button type="link" onClick={() => setDetail(row)}>详情</Button>
          <Button
            type="link"
            loading={solve.isPending && solve.variables?.challenge === row.id}
            disabled={!canStartSolve}
            onClick={() => groundId && solve.mutate({ ground: groundId, challenge: row.id })}
          >
            跑题
          </Button>
          <Button
            type="link"
            loading={enqueueIds.isPending}
            disabled={!groundId}
            onClick={() =>
              groundId && enqueueIds.mutate({ practice_ground_id: groundId, challenge_ids: [row.id], priority: 0 })
            }
          >
            入队
          </Button>
        </Space>
      ),
    },
  ];

  const queueColumns: ColumnsType<ArenaQueueItem> = [
    {
      title: "题目",
      dataIndex: "challenge_name",
      ellipsis: true,
      render: (value: string, row: ArenaQueueItem) => (
        <Space size={6}>
          <span>{value || row.platform_challenge_id}</span>
          {row.status === "SOLVED" && row.verdict === "accepted" ? <Tag color="success">accepted</Tag> : null}
        </Space>
      ),
    },
    { title: "题型", dataIndex: "category", width: 100, render: (value: string) => value ? <Tag>{value}</Tag> : "—" },
    { title: "难度", dataIndex: "difficulty", width: 80, render: (value: string) => value || "—" },
    {
      title: "环境",
      width: 90,
      render: (_: unknown, row: ArenaQueueItem) =>
        row.has_container ? <Tag color="processing">需靶机</Tag> : <Tag>离线</Tag>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: ArenaQueueItem["status"]) => <Tag color={QUEUE_TAG_COLOR[value]}>{QUEUE_LABEL[value]}</Tag>,
    },
    { title: "优先级", dataIndex: "priority", width: 80 },
    {
      title: "尝试",
      width: 110,
      render: (_: unknown, row: ArenaQueueItem) => (
        <Tooltip title="实际解题次数 / 被调度次数。进程重启造成的重新调度不计入重试预算，所以两者会不同。">
          <span>
            <span style={{ color: row.outcome_count >= 2 ? "#c0392b" : undefined }}>
              {row.outcome_count}
            </span>
            <span style={{ color: "#8c8c8c" }}> / {row.attempt_count}</span>
          </span>
        </Tooltip>
      ),
    },
    {
      title: "耗时",
      width: 100,
      render: (_: unknown, row: ArenaQueueItem) =>
        row.started_at && row.finished_at
          ? `${Math.round((new Date(row.finished_at).getTime() - new Date(row.started_at).getTime()) / 1000)}s`
          : "—",
    },
    {
      title: "备注",
      dataIndex: "last_error",
      ellipsis: true,
      render: (value: string | null) => value ? <span style={{ color: "#c0392b", fontSize: 12 }}>{value}</span> : "—",
    },
    {
      title: "Run",
      width: 90,
      render: (_: unknown, row: ArenaQueueItem) =>
        row.run_id ? <Link to={`/runs/${row.run_id}`}>查看</Link> : <span style={{ color: "#8c8c8c" }}>—</span>,
    },
    {
      title: "操作",
      width: 190,
      fixed: "right" as const,
      render: (_: unknown, row: ArenaQueueItem) => {
        const finished = ["SOLVED", "UNSOLVED", "FAILED", "CANCELLED"].includes(row.status);
        return (
          <Space size={0} wrap>
            <Button
              type="link"
              disabled={finished}
              loading={prioritizeItem.isPending && prioritizeItem.variables?.id === row.id}
              onClick={() => prioritizeItem.mutate({ id: row.id, position: "top" })}
            >
              置顶
            </Button>
            {finished ? (
              <Button
                type="link"
                loading={bulkQueue.isPending}
                onClick={() => bulkQueue.mutate({ item_ids: [row.id], action: "retry" })}
              >
                重试
              </Button>
            ) : (
              <Button
                type="link"
                danger
                loading={cancelItem.isPending && cancelItem.variables === row.id}
                onClick={() => cancelItem.mutate(row.id)}
              >
                取消
              </Button>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Agent 竞技场</h1>
          <p>
            调度竞技场批量解题、盯住唯一的环境槽位与调度器心跳。靶机、提交与结果都在这里闭环，
            不再需要另开终端。
          </p>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={() => { void status.refetch(); void connections.refetch(); void queue.refetch(); }}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => edit()}>新建连接</Button>
        </Space>
      </div>

      {!activeConnection ? (
        <Alert
          className="panel-card"
          type="warning"
          showIcon
          icon={<KeyOutlined />}
          message="尚未启用任何竞技场连接"
          description="先新建并启用一条连接，再设置默认解题模型。竞技场按 Agent 限流，且同时只允许一个运行中的靶机环境。"
        />
      ) : null}

      <Card
        className="panel-card"
        title={<Space><ThunderboltOutlined />运行控制台</Space>}
        extra={<span style={{ color: "#8c8c8c", fontSize: 12 }}>状态每 30 秒刷新，队列每 5 秒刷新</span>}
        style={{ marginTop: 18 }}
      >
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}>
            <Card size="small" title="调度器" style={{ height: "100%" }}>
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Space wrap>
                  <SchedulerTag scheduler={scheduler} />
                  {scheduler?.consecutive_failures ? (
                    <Tooltip title={scheduler.last_error ?? ""}>
                      <Tag color="error">连续失败 {scheduler.consecutive_failures} 次</Tag>
                    </Tooltip>
                  ) : null}
                  {scheduler?.watchdog_timeouts ? (
                    <Tag color="warning">看门狗超时 {scheduler.watchdog_timeouts} 次</Tag>
                  ) : null}
                  {scheduler ? <Tag>已处理 {scheduler.processed_items} 题</Tag> : null}
                </Space>
                <Space wrap>
                  <Button
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    loading={startQueue.isPending}
                    disabled={scheduler?.running || !(counts.PENDING ?? 0)}
                    onClick={() => startQueue.mutate()}
                  >
                    开始执行
                  </Button>
                  <Button
                    icon={<PauseCircleOutlined />}
                    loading={stopQueue.isPending}
                    disabled={!scheduler?.running}
                    onClick={() => stopQueue.mutate()}
                  >
                    停止（做完当前题）
                  </Button>
                  <Button
                    icon={<SyncOutlined />}
                    loading={recoverQueue.isPending}
                    onClick={() => recoverQueue.mutate()}
                  >
                    修复滞留任务
                  </Button>
                </Space>
                {scheduler?.last_error ? (
                  <Alert type="error" showIcon message={scheduler.last_error} />
                ) : null}
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card size="small" title="环境槽位（平台全局容量 1）" style={{ height: "100%" }}>
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    {
                      key: "active",
                      label: "平台报告占用",
                      children: `${orphans.data?.active_environments ?? "?"} / ${orphans.data?.max_environments ?? "?"}`,
                    },
                    { key: "local", label: "本地持有", children: heldLease ?? "空闲" },
                    {
                      key: "orphan",
                      label: "疑似泄漏",
                      // `orphan_suspected` only says the platform reports an
                      // environment -- which is exactly the state of a solve that
                      // is running normally. It is a leak only when the platform
                      // reports one and this console holds no lease for it.
                      children:
                        orphans.data?.orphan_suspected && !heldLease ? (
                          <Tag color="error">有占用但本地无租约，需回收</Tag>
                        ) : orphans.data?.orphan_suspected ? (
                          <Tag color="processing">占用中（本机正在解题）</Tag>
                        ) : (
                          <Tag color="success">无</Tag>
                        ),
                    },
                  ]}
                />
                <Space wrap>
                  <Button
                    icon={<SecurityScanOutlined />}
                    disabled={!activeConnection}
                    onClick={() => setReclaimOpen(true)}
                  >
                    回收泄漏靶机
                  </Button>
                  <Button
                    icon={<ReloadOutlined />}
                    loading={orphans.isFetching}
                    disabled={!activeConnection}
                    onClick={() => void orphans.refetch()}
                  >
                    重新核对
                  </Button>
                </Space>
              </Space>
            </Card>
          </Col>
        </Row>
        <Divider style={{ margin: "16px 0 12px" }} />
        <Space wrap>
          {(["PENDING", "RUNNING", "SOLVED", "UNSOLVED", "FAILED", "CANCELLED"] as const).map((key) =>
            counts[key] ? (
              <Tag key={key} color={QUEUE_TAG_COLOR[key]}>{QUEUE_LABEL[key]} {counts[key]}</Tag>
            ) : null,
          )}
          {!Object.keys(counts).length ? <span style={{ color: "#8c8c8c" }}>队列为空</span> : null}
        </Space>
      </Card>

      {solveTaskId ? (
        <Card
          className="panel-card"
          title={<Space><TrophyOutlined />单题跑题结果</Space>}
          extra={<Button type="link" onClick={() => setSolveTaskId(undefined)}>收起</Button>}
          style={{ marginTop: 18 }}
        >
          {solveTask.data?.state === "running" ? (
            <Space><Spin /><span>解题进行中…靶机已租用，任务会在结束时自动销毁靶机。</span></Space>
          ) : solveTask.data?.state === "failed" ? (
            <Alert type="error" showIcon message={solveTask.data.error ?? "解题任务失败"} />
          ) : solveTask.data?.outcome ? (
            <>
              <Descriptions
                size="small"
                bordered
                column={{ xs: 1, md: 2 }}
                items={[
                  { key: "run", label: "任务状态", children: solveTask.data.outcome.run_status || "—" },
                  {
                    key: "runid",
                    label: "Run",
                    children: solveTask.data.outcome.run_id
                      ? <Link to={`/runs/${solveTask.data.outcome.run_id}`}>{solveTask.data.outcome.run_id}</Link>
                      : "—",
                  },
                  { key: "verdict", label: "判定", children: <VerdictTag verdict={solveTask.data.outcome.verdict ?? "unknown"} /> },
                  {
                    key: "target",
                    label: "靶机",
                    children: String((solveTask.data.outcome.environment as { access_url?: string })?.access_url ?? "—"),
                  },
                ]}
              />
              {solveTask.data.outcome.flag_candidates.length ? (
                <Table
                  size="small"
                  style={{ marginTop: 12 }}
                  rowKey="flag"
                  pagination={false}
                  dataSource={solveTask.data.outcome.flag_candidates.map((flag) => ({ flag }))}
                  columns={[
                    {
                      title: "候选 flag",
                      dataIndex: "flag",
                      ellipsis: true,
                      render: (value: string) => <CopyText text={value} />,
                    },
                  ]}
                />
              ) : (
                <Alert type="info" showIcon style={{ marginTop: 12 }} message="本次未提取到 flag 候选。" />
              )}
              {solveTask.data.outcome.notes?.length ? (
                <div style={{ marginTop: 12 }}>
                  {solveTask.data.outcome.notes.map((note, index) => (
                    <div key={index} className="event-payload">{note}</div>
                  ))}
                </div>
              ) : null}
            </>
          ) : null}
        </Card>
      ) : null}

      <Tabs
        className="panel-card"
        activeKey={tab}
        onChange={setTab}
        style={{ marginTop: 18 }}
        items={[
          {
            key: "catalogue",
            label: <Space size={6}><BookOutlined />题库</Space>,
            children: !activeConnection ? (
              <Empty description="先配置并启用一条竞技场连接" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <>
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 14 }}
                  message="跑题会占用平台唯一的环境槽位（约 1 小时）：起靶机 → 用默认模型解题 → 提交 flag → 自动销毁。解题期间请勿启动第二次。"
                />
                {!activeConnection.default_model_config_id ? (
                  <Alert
                    type="error"
                    showIcon
                    style={{ marginBottom: 14 }}
                    message="尚未设置默认模型，跑题会被拒绝。请先在「连接」页签的编辑里选择默认模型。"
                  />
                ) : null}
                <Space wrap style={{ marginBottom: 14 }}>
                  <Select
                    placeholder="选择练习场"
                    style={{ minWidth: 260 }}
                    value={groundId}
                    onChange={setGroundId}
                    loading={grounds.isLoading}
                    options={groundOptions}
                  />
                  <Select
                    allowClear
                    placeholder="题型"
                    style={{ minWidth: 150 }}
                    value={catalogueCategory}
                    onChange={setCatalogueCategory}
                    options={facets.categories.map((value) => ({ value, label: value }))}
                  />
                  <Select
                    allowClear
                    placeholder="难度"
                    style={{ minWidth: 130 }}
                    value={catalogueDifficulty}
                    onChange={setCatalogueDifficulty}
                    options={facets.difficulties.map((value) => ({ value, label: value }))}
                  />
                  <Space size={6}>
                    <Switch size="small" checked={catalogueUnsolvedOnly} onChange={setCatalogueUnsolvedOnly} />
                    <span style={{ fontSize: 12, color: "#5b827c" }}>只看未解</span>
                  </Space>
                  <Input
                    allowClear
                    prefix={<SearchOutlined />}
                    placeholder="在本页内搜索题目"
                    style={{ width: 220 }}
                    value={catalogueSearch}
                    onChange={(event) => setCatalogueSearch(event.target.value)}
                  />
                  <Button icon={<ReloadOutlined />} loading={mcpChallenges.isFetching} disabled={!groundId} onClick={() => void mcpChallenges.refetch()}>
                    刷新
                  </Button>
                </Space>
                {mcpChallenges.data ? (
                  <Space wrap style={{ marginBottom: 14 }}>
                    <Tag color="blue">共 {mcpChallenges.data.total ?? catalogueItems.length} 题</Tag>
                    <Tag color="orange">本页需靶机 {mcpChallenges.data.container_count}</Tag>
                    <Tag>本页带附件 {mcpChallenges.data.attachment_count}</Tag>
                    {mcpChallenges.data.total_page ? (
                      <Tag>第 {mcpChallenges.data.page ?? cataloguePage} / {mcpChallenges.data.total_page} 页</Tag>
                    ) : null}
                    {catalogueSearch.trim() ? <Tag color="purple">本地命中 {filteredCatalogue.length} 条</Tag> : null}
                  </Space>
                ) : null}
                <Space wrap style={{ marginBottom: 14 }}>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    disabled={!selectedChallenges.length}
                    loading={enqueueIds.isPending}
                    onClick={() =>
                      groundId &&
                      enqueueIds.mutate({ practice_ground_id: groundId, challenge_ids: selectedChallenges, priority: 0 })
                    }
                  >
                    加入队列（选中 {selectedChallenges.length}）
                  </Button>
                  <Button icon={<UnorderedListOutlined />} onClick={() => setPasteOpen(true)} disabled={!groundId}>
                    粘贴 ID 入队
                  </Button>
                  <InputNumber min={1} max={100} value={enqueueLimit} onChange={(value) => setEnqueueLimit(value ?? 10)} style={{ width: 110 }} addonBefore="数量" />
                  <Select
                    value={enqueueOrder}
                    onChange={setEnqueueOrder}
                    style={{ width: 160 }}
                    options={[
                      { value: "container_first", label: "先靶机题" },
                      { value: "offline_first", label: "先离线题" },
                      { value: "as_listed", label: "按原顺序" },
                    ]}
                  />
                  <Tooltip title="按当前题型/难度筛选，在平台上翻页取够数量后整批入队">
                    <Button
                      icon={<DeploymentUnitOutlined />}
                      loading={enqueue.isPending}
                      disabled={!groundId}
                      onClick={() =>
                        groundId &&
                        enqueue.mutate({
                          practice_ground_id: groundId,
                          category: catalogueCategory,
                          difficulty: catalogueDifficulty,
                          order: enqueueOrder,
                          // Always on: re-solving a challenge the platform already
                          // reports as solved burns the single environment slot for a
                          // result that cannot improve.
                          skip_solved: true,
                          limit: enqueueLimit,
                        })
                      }
                    >
                      按筛选整批入队
                    </Button>
                  </Tooltip>
                </Space>
                <Table
                  className="cyber-table"
                  rowKey="id"
                  size="small"
                  loading={mcpChallenges.isLoading}
                  dataSource={filteredCatalogue}
                  scroll={{ x: 1180 }}
                  tableLayout="fixed"
                  pagination={false}
                  rowSelection={{
                    selectedRowKeys: selectedChallenges,
                    onChange: (keys) => setSelectedChallenges(keys.map(String)),
                    preserveSelectedRowKeys: true,
                  }}
                  locale={{
                    emptyText: (
                      <Empty
                        description={groundId ? "该筛选条件下暂无题目" : "请先选择练习场"}
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                      />
                    ),
                  }}
                  columns={catalogueColumns}
                />
                {catalogueTotalPages > 1 ? (
                  <Pagination
                    style={{ marginTop: 14, textAlign: "right" }}
                    current={cataloguePage}
                    pageSize={CATALOGUE_PAGE_SIZE}
                    total={catalogueTotalPages * CATALOGUE_PAGE_SIZE}
                    showSizeChanger={false}
                    onChange={(page) => setCataloguePage(page)}
                  />
                ) : null}
              </>
            ),
          },
          {
            key: "queue",
            label: <Space size={6}><UnorderedListOutlined />排队</Space>,
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 14 }}
                  message="批量执行会长时间占用平台唯一的环境槽位（每题最长约 1 小时）。调度器只读「待执行」的行，所以卡在「执行中」的滞留任务要用「修复滞留任务」退回。"
                />
                <Space wrap style={{ marginBottom: 14 }}>
                  <Select
                    allowClear
                    placeholder="全部状态"
                    style={{ width: 150 }}
                    value={queueStatus}
                    onChange={setQueueStatus}
                    options={Object.entries(QUEUE_LABEL).map(([value, label]) => ({
                      value,
                      label: `${label}${counts[value] ? `（${counts[value]}）` : ""}`,
                    }))}
                  />
                  <Input
                    allowClear
                    prefix={<SearchOutlined />}
                    placeholder="搜索题目 / 题型"
                    style={{ width: 220 }}
                    value={queueSearch}
                    onChange={(event) => setQueueSearch(event.target.value)}
                  />
                  <Space size={6}>
                    <Select
                      value={clearStatus}
                      onChange={setClearStatus}
                      style={{ width: 130 }}
                      options={Object.entries(QUEUE_LABEL).map(([value, label]) => ({ value, label }))}
                    />
                    <Popconfirm
                      title={`清空全部「${QUEUE_LABEL[clearStatus] ?? clearStatus}」记录？`}
                      description="清空是删除，不可恢复。"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={() => clearQueue.mutate()}
                    >
                      <Button danger icon={<DeleteOutlined />} loading={clearQueue.isPending}>清空</Button>
                    </Popconfirm>
                  </Space>
                </Space>
                <Space wrap style={{ marginBottom: 14 }}>
                  <span style={{ color: "#5b827c", fontSize: 12 }}>已选 {selectedQueue.length} 条：</span>
                  <Button
                    icon={<RocketOutlined />}
                    disabled={!selectedQueue.length}
                    loading={bulkQueue.isPending}
                    onClick={() => bulkQueue.mutate({ item_ids: selectedQueue, action: "retry" })}
                  >
                    重试
                  </Button>
                  <Button
                    icon={<StarOutlined />}
                    disabled={!selectedQueue.length}
                    loading={bulkQueue.isPending}
                    onClick={() => bulkQueue.mutate({ item_ids: selectedQueue, action: "top" })}
                  >
                    置顶
                  </Button>
                  <Button
                    icon={<PauseCircleOutlined />}
                    disabled={!selectedQueue.length}
                    loading={bulkQueue.isPending}
                    onClick={() => bulkQueue.mutate({ item_ids: selectedQueue, action: "cancel" })}
                  >
                    取消
                  </Button>
                  <Popconfirm
                    title={`删除选中的 ${selectedQueue.length} 条记录？`}
                    description="只删除已结束的行，执行中的会被跳过。"
                    okText="删除"
                    cancelText="取消"
                    onConfirm={() => bulkQueue.mutate({ item_ids: selectedQueue, action: "delete" })}
                  >
                    <Button danger icon={<DeleteOutlined />} disabled={!selectedQueue.length}>删除</Button>
                  </Popconfirm>
                </Space>
                <Table
                  className="cyber-table"
                  rowKey="id"
                  size="small"
                  loading={queue.isLoading}
                  dataSource={queueItems}
                  scroll={{ x: 1480 }}
                  tableLayout="fixed"
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  rowSelection={{
                    selectedRowKeys: selectedQueue,
                    onChange: (keys) => setSelectedQueue(keys.map(String)),
                    preserveSelectedRowKeys: true,
                  }}
                  locale={{ emptyText: <Empty description="队列为空" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                  columns={queueColumns}
                />
              </>
            ),
          },
          {
            key: "score",
            label: <Space size={6}><TrophyOutlined />战绩</Space>,
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 14 }}
                  message="平台不提供历史战绩，以下数据来自本地记账。按题型与归因分别统计——只报总分无法区分模型强弱。"
                />
                <Row gutter={[18, 18]}>
                  <Col xs={12} md={6}><Card size="small"><Statistic title="提交次数" value={stats?.total.attempted ?? 0} /></Card></Col>
                  <Col xs={12} md={6}><Card size="small"><Statistic title="通过" value={stats?.total.accepted ?? 0} valueStyle={{ color: "#3f8600" }} /></Card></Col>
                  <Col xs={12} md={6}><Card size="small"><Statistic title="触及题目" value={stats?.distinct_challenges_touched ?? 0} /></Card></Col>
                  <Col xs={12} md={6}><Card size="small"><Statistic title="解出题目" value={stats?.distinct_challenges_solved ?? 0} /></Card></Col>
                </Row>
                <Row gutter={[18, 18]} style={{ marginTop: 18 }}>
                  {([
                    ["by_category", "按题型"],
                    ["by_model", "按模型（归因）"],
                    ["by_engine", "按引擎（归因）"],
                  ] as const).map(([key, title]) => {
                    const rows = bucketRows((stats?.[key] ?? {}) as Record<string, ArenaBucket>);
                    return (
                      <Col xs={24} lg={8} key={key}>
                        <Card size="small" title={title}>
                          <Table
                            size="small"
                            rowKey="key"
                            pagination={false}
                            dataSource={rows}
                            columns={[
                              {
                                title: title.includes("模型") ? "模型" : title.includes("引擎") ? "引擎" : "分类",
                                dataIndex: "key",
                                ellipsis: true,
                                render: (value: string, row: ArenaBucket & { key: string }) => row.label ?? value,
                              },
                              ...bucketColumns,
                            ]}
                            locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                          />
                        </Card>
                      </Col>
                    );
                  })}
                </Row>
                <Card size="small" title={<Space><CloudDownloadOutlined />提交记录</Space>} style={{ marginTop: 18 }}>
                  <Table
                    className="cyber-table"
                    rowKey="id"
                    size="small"
                    dataSource={submissions.data ?? []}
                    loading={submissions.isLoading}
                    scroll={{ x: 1200 }}
                    tableLayout="fixed"
                    locale={{ emptyText: <Empty description="暂无提交记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                    columns={submissionColumns}
                  />
                </Card>
              </>
            ),
          },
          {
            key: "connections",
            label: <Space size={6}><ApiOutlined />连接与诊断</Space>,
            children: (
              <>
                <Card
                  size="small"
                  title={<Space><DatabaseOutlined />连接清单</Space>}
                  extra={<span style={{ color: "#8c8c8c", fontSize: 12 }}>Token 只写入后端并加密存储</span>}
                >
                  <Table
                    className="cyber-table"
                    rowKey="id"
                    size="small"
                    dataSource={connections.data ?? []}
                    loading={connections.isLoading}
                    scroll={{ x: 1400 }}
                    tableLayout="fixed"
                    locale={{ emptyText: <Empty description="尚未配置竞技场连接" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                    columns={connectionColumns}
                  />
                </Card>
                <Card
                  size="small"
                  title={<Space><ExperimentOutlined />运行状态自检</Space>}
                  style={{ marginTop: 18 }}
                >
                  {status.isLoading ? (
                    <Spin />
                  ) : (
                    <>
                      <Table
                        size="small"
                        rowKey="name"
                        pagination={false}
                        dataSource={status.data?.checks ?? []}
                        columns={[
                          { title: "检查项", dataIndex: "name", width: 240 },
                          { title: "结果", dataIndex: "ok", width: 100, render: (value: boolean) => <CheckTag ok={value} /> },
                          { title: "说明", dataIndex: "message" },
                        ]}
                      />
                      {status.data?.connections.map((item) => (
                        <Descriptions
                          key={item.id}
                          size="small"
                          bordered
                          column={{ xs: 1, md: 3 }}
                          style={{ marginTop: 14 }}
                          title={item.name}
                          items={[
                            {
                              key: "profile",
                              label: "Agent 档案",
                              span: 2,
                              children: Object.keys(item.profile_json ?? {}).length
                                ? JSON.stringify(item.profile_json)
                                : "尚未探测",
                            },
                            { key: "pace", label: "本地限流", children: `${item.requests_per_minute} rpm / 并发 ${item.max_concurrency}` },
                          ]}
                        />
                      ))}
                    </>
                  )}
                </Card>
                <RESTCatalogueDiagnostic connection={activeConnection} />
              </>
            ),
          },
        ]}
      />

      <Drawer
        open={Boolean(detail)}
        width={560}
        title={detail?.name ?? "题目详情"}
        onClose={() => setDetail(undefined)}
      >
        {detail ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions
              size="small"
              bordered
              column={1}
              items={[
                { key: "friendly", label: "题号", children: detail.friendly_id || "—" },
                {
                  key: "id",
                  label: "平台 ID",
                  children: <CopyText text={detail.id} />,
                },
                { key: "category", label: "题型", children: detail.category || "—" },
                { key: "difficulty", label: "难度", children: detail.difficulty || "—" },
                { key: "points", label: "分值", children: detail.points ?? "—" },
                {
                  key: "env",
                  label: "环境",
                  children: detail.has_container ? "需要靶机（占用唯一槽位）" : "离线/仅附件",
                },
                { key: "solved", label: "平台已解", children: detail.is_solved ? "是" : "否" },
                { key: "solve_count", label: "解出数", children: detail.solve_count ?? "—" },
              ]}
            />
            <Space wrap>
              <Button
                type="primary"
                icon={<RocketOutlined />}
                disabled={!canStartSolve || !groundId}
                loading={solve.isPending}
                onClick={() => groundId && solve.mutate({ ground: groundId, challenge: detail.id })}
              >
                立即跑题
              </Button>
              <Button
                icon={<PlusOutlined />}
                disabled={!groundId}
                loading={enqueueIds.isPending}
                onClick={() =>
                  groundId &&
                  enqueueIds.mutate({ practice_ground_id: groundId, challenge_ids: [detail.id], priority: 0 })
                }
              >
                加入队列
              </Button>
              {detail.has_container ? (
                <Button
                  danger
                  icon={<WarningOutlined />}
                  onClick={() => {
                    setReclaimChallengeId(detail.id);
                    setReclaimOpen(true);
                  }}
                >
                  销毁它的靶机
                </Button>
              ) : null}
            </Space>
            <DetailPayload
              connectionId={activeConnection?.id}
              groundId={groundId}
              challengeId={detail.id}
            />
          </Space>
        ) : null}
      </Drawer>

      <Modal
        open={pasteOpen}
        title="粘贴题目 ID 入队"
        okText="入队"
        confirmLoading={enqueueIds.isPending}
        onCancel={() => setPasteOpen(false)}
        onOk={() => {
          const ids = pasteIds
            .split(/[\s,;]+/)
            .map((value) => value.trim())
            .filter(Boolean);
          if (!ids.length) {
            message.warning("请先粘贴至少一个 ID");
            return;
          }
          if (!groundId) {
            message.warning("请先选择练习场");
            return;
          }
          enqueueIds.mutate({ practice_ground_id: groundId, challenge_ids: ids.slice(0, 50), priority: 0 });
        }}
      >
        <p style={{ marginTop: 0, color: "#5b827c", fontSize: 12 }}>
          每个 ID 会单独向平台读取一次，因此不受目录分页限制——第 2 页之后的题也能入队。
          最多 50 个，平台读不到的 ID 会单独列出来。
        </p>
        <Input.TextArea
          rows={6}
          value={pasteIds}
          onChange={(event) => setPasteIds(event.target.value)}
          placeholder={"每行一个，或用空格/逗号分隔\nc34d755f-edd6-47cb-94f6-78c45b3a8b4f\n8f2e44f8-85c3-470b-a232-dc53e6644109"}
        />
      </Modal>

      <Modal
        open={reclaimOpen}
        title="回收泄漏的靶机"
        okText="销毁"
        okButtonProps={{ danger: true }}
        confirmLoading={reclaim.isPending}
        onCancel={() => setReclaimOpen(false)}
        onOk={() => reclaim.mutate(reclaimChallengeId)}
      >
        <p style={{ marginTop: 0 }}>
          平台每个 Agent 只允许 <strong>一个</strong> 运行中的靶机。崩溃中断留下的靶机不会被自动回收，
          后续所有解题都会失败在看起来无关的地方。
        </p>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="销毁顺序：优先用上次记录的租约（本地崩溃前写入）；记录缺失时可手动指定题目。"
        />
        <Input
          placeholder="（可选）题目 ID，覆盖本地记录"
          value={reclaimChallengeId}
          onChange={(event) => setReclaimChallengeId(event.target.value || undefined)}
          addonBefore="Challenge"
        />
      </Modal>

      <Modal
        open={open}
        title={editing ? "编辑竞技场连接" : "新建竞技场连接"}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={save.isPending}
        width={620}
      >
        <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="DASCTF 竞技场" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true, type: "url" }]}>
            <Input placeholder={DEFAULT_BASE_URL} />
          </Form.Item>
          <Form.Item
            name="token"
            label={editing ? "新 Token（留空则不变）" : "Agent Token"}
            rules={editing ? [] : [{ required: true }]}
            extra="请求头 Authorization: Bearer <token>，由平台 Agent 档案页签发"
          >
            <Input.Password autoComplete="new-password" placeholder="粘贴 Agent Token" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="requests_per_minute" label="本地限流（rpm）" extra="平台按 Agent 限流，此处只做本地节流">
                <InputNumber min={1} max={600} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="max_concurrency" label="最大并发" extra="平台同时只允许 1 个靶机，默认串行">
                <InputNumber min={1} max={4} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item
            name="default_model_config_id"
            label="默认解题模型"
            extra="竞技场跑题时使用的模型；不设置则跑题会被拒绝"
          >
            <Select
              allowClear
              placeholder="选择模型配置"
              options={(models.data ?? [])
                .filter((item) => item.enabled)
                .map((item) => ({ value: item.id, label: `${item.name}（${item.provider_type}）` }))}
            />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="default_engine_type" label="默认引擎" extra="留空则用模型的提供方">
                <Select allowClear placeholder="跟随模型" options={[
                  { value: "openai_compatible", label: "openai_compatible" },
                  { value: "codex_cli", label: "codex_cli" },
                  { value: "claude_cli", label: "claude_cli" },
                ]} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="default_solver_mode" label="解题模式">
                <Select options={[
                  { value: "muteki", label: "muteki（多智能体）" },
                  { value: "single_agent", label: "single_agent（单智能体）" },
                ]} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="default_reason_model_config_id"
            label="协作模型（Coordinator）"
            extra="Muteki 多引擎模式下负责推理与决策；留空则用本地默认。仅支持 OpenAI 协议模型"
          >
            <Select
              allowClear
              placeholder="选择协作模型"
              options={(models.data ?? [])
                .filter((item) => item.enabled && item.provider_type !== "codex_cli" && item.provider_type !== "claude_cli" && item.provider_type !== "cursor_cli")
                .map((item) => ({ value: item.id, label: `${item.name}（${item.provider_type}）` }))}
            />
          </Form.Item>
          <Form.Item
            label="多引擎 Worker"
            extra="留空则只用上面的「默认解题模型」。每行一个引擎，第一个即主引擎"
          >
            <Form.List name="default_worker_engines">
              {(fields, { add, remove }) => (
                <>
                  {fields.map((field) => (
                    <Row key={field.key} gutter={8} style={{ marginBottom: 8 }}>
                      <Col span={9}>
                        <Form.Item name={[field.name, "engine_type"]} noStyle>
                          <Select
                            placeholder="引擎"
                            options={[
                              { value: "claude_cli", label: "claude_cli" },
                              { value: "codex_cli", label: "codex_cli" },
                              { value: "openai_compatible", label: "openai_compatible" },
                            ]}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={13}>
                        <Form.Item name={[field.name, "model_config_id"]} noStyle>
                          <Select
                            allowClear
                            placeholder="模型配置"
                            options={(models.data ?? [])
                              .filter((item) => item.enabled)
                              .map((item) => ({ value: item.id, label: `${item.name}（${item.provider_type}）` }))}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={2}>
                        <Button type="text" danger onClick={() => remove(field.name)}>删</Button>
                      </Col>
                    </Row>
                  ))}
                  <Button type="dashed" block onClick={() => add({ engine_type: "claude_cli" })}>
                    + 添加 Worker 引擎
                  </Button>
                </>
              )}
            </Form.List>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={Boolean(removing)}
        title={`删除竞技场连接：${removing?.name ?? ""}`}
        okText="删除"
        okButtonProps={{ danger: true, loading: remove.isPending }}
        cancelText="取消"
        onCancel={() => setRemoving(undefined)}
        onOk={() => removing && remove.mutate(removing.id)}
      >
        <p style={{ marginTop: 0 }}>删除后无法恢复，该连接下的本地提交记录会一并删除。</p>
        <Alert type="warning" showIcon message="平台侧数据不受影响，但本地归因统计将无法还原。" />
      </Modal>
    </>
  );
}

// The full challenge payload, fetched on demand. Kept in its own component so the
// drawer opens instantly from the row it already has and only the extra detail
// costs a platform read.
function DetailPayload({
  connectionId,
  groundId,
  challengeId,
}: {
  connectionId?: string;
  groundId?: string;
  challengeId: string;
}) {
  const detail = useQuery({
    queryKey: ["arena-mcp-challenge", connectionId, groundId, challengeId],
    queryFn: () => api.getArenaMcpChallenge(connectionId!, groundId!, challengeId),
    enabled: Boolean(connectionId && groundId),
  });
  if (detail.isLoading) return <Spin />;
  if (detail.error) return <Alert type="error" showIcon message={(detail.error as Error).message} />;
  if (!detail.data) return null;
  const challenge = (detail.data.challenge ?? {}) as Record<string, unknown>;
  const description = String(challenge.description ?? "");
  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <div className="muteki-subtle">平台返回的完整题目对象（只读，不落库）</div>
      {description ? (
        <Alert type="info" showIcon message="题目描述" description={<span className="event-payload">{description}</span>} />
      ) : null}
      <pre className="event-payload">{JSON.stringify(detail.data, null, 2)}</pre>
    </Space>
  );
}

// The REST catalogue is the arena's only non-MCP read route. It cannot page and
// cannot see past its first 100 rows, so it is a diagnostic rather than a
// browsing surface -- which is exactly why it lives here and not in the
// catalogue tab, where its limits would read as the catalogue's.
function RESTCatalogueDiagnostic({ connection }: { connection?: ArenaConnection }) {
  const [category, setCategory] = useState<string>();
  const [limit, setLimit] = useState(50);
  const challenges = useQuery({
    queryKey: ["arena-challenges", connection?.id, category, limit],
    queryFn: () => api.listArenaChallenges(connection!.id, category, limit),
    enabled: Boolean(connection),
  });
  if (!connection) {
    return (
      <Card size="small" title={<Space><BookOutlined />REST 目录（诊断）</Space>} style={{ marginTop: 18 }}>
        <Empty description="先配置并启用一条竞技场连接" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    );
  }
  return (
    <Card
      size="small"
      title={<Space><BookOutlined />REST 目录（诊断）</Space>}
      extra={<span style={{ color: "#8c8c8c", fontSize: 12 }}>不支持翻页，最多前 100 条</span>}
      style={{ marginTop: 18 }}
    >
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          allowClear
          placeholder="题型"
          style={{ minWidth: 180 }}
          value={category}
          onChange={setCategory}
          options={[...new Set(Object.keys(challenges.data?.categories ?? {}))].map((key) => ({
            value: key,
            label: `${key}（${challenges.data?.categories[key]}）`,
          }))}
        />
        <Select
          value={limit}
          onChange={setLimit}
          style={{ width: 130 }}
          options={[10, 25, 50, 100].map((value) => ({ value, label: `每页 ${value} 条` }))}
        />
        <Button icon={<ReloadOutlined />} loading={challenges.isFetching} onClick={() => void challenges.refetch()}>
          刷新
        </Button>
        {challenges.data ? (
          <Tag color="blue">
            已取 {challenges.data.items.length} 条{challenges.data.total ? ` / 共 ${challenges.data.total}` : ""}
          </Tag>
        ) : null}
        {challenges.data && !challenges.data.paging_supported ? <Tag color="warning">平台忽略 offset</Tag> : null}
      </Space>
      <Table<ArenaChallenge>
        className="cyber-table"
        rowKey="id"
        size="small"
        loading={challenges.isLoading}
        dataSource={challenges.data?.items ?? []}
        scroll={{ x: 900 }}
        tableLayout="fixed"
        pagination={false}
        locale={{ emptyText: <Empty description="暂无题目" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        columns={[
          { title: "题号", dataIndex: "friendly_id", width: 150, ellipsis: true, render: (value: string) => value || "—" },
          { title: "题目", dataIndex: "name", ellipsis: true, render: (value: string) => value || "—" },
          { title: "题型", dataIndex: "category", width: 140, render: (value: string) => value ? <Tag>{value}</Tag> : "—" },
          { title: "难度", dataIndex: "difficulty", width: 90, render: (value: string) => value || "—" },
          { title: "分值", dataIndex: "points", width: 70, render: (value: number) => value ?? "—" },
          {
            title: "环境",
            width: 100,
            render: (_: unknown, item: ArenaChallenge) =>
              item.has_container ? <Tag color="processing">需靶机</Tag> : <Tag>仅附件</Tag>,
          },
        ]}
      />
    </Card>
  );
}
