import { ApiOutlined, CloudServerOutlined, CodeOutlined, KeyOutlined, PlusOutlined, RobotOutlined, SafetyCertificateOutlined, ToolOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  message,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
} from "antd";
import { useState } from "react";
import type { ReactNode } from "react";
import { ModelSkillBinding } from "../components/skills/ModelSkillBinding";
import { api } from "../services/api";
import type { ModelConfig, ModelProviderType } from "../types/api";

// ``runner_url`` is optional: with the legacy Runner retired the dialog does not
// show the field, so the payload carries only what the operator actually edited.
type ServiceForm = { runner_url?: string; default_model_config_id?: string | null };
type SkillBinding = { skill_id: string; enabled: boolean; priority: number; config_json: Record<string, unknown> };
type ModelConfigFormValues = {
  name?: string;
  provider_type?: ModelConfig["provider_type"];
  base_url?: string;
  // Codex is pinned to "responses"; an OpenAI-wire provider may pick either.
  wire_api?: "responses" | "chat_completions";
  model_name?: string;
  reasoning_effort?: ModelConfig["reasoning_effort"];
  api_key?: string;
  enabled?: boolean;
  roles?: ModelConfig["roles"];
  action_protocol?: string;
  structured_output_mode?: string;
  request_timeout_seconds?: number;
  max_output_tokens?: number;
  temperature?: number;
  max_retries?: number;
  retry_base_seconds?: number;
  rate_limit_cooldown_seconds?: number;
  requests_per_minute?: number;
  max_concurrency?: number;
  context_token_limit?: number;
  // Routing controls for a claude_cli Worker: send its traffic through the
  // local protocol-translating proxy, and the official-name -> upstream-model
  // map the proxy resolves against. A relay that refuses /v1/messages (many
  // groups do) can only be reached this way.
  route_via_proxy?: boolean;
  model_aliases_text?: string;
};

const modelConfigFields: Array<keyof ModelConfigFormValues> = [
  "name", "provider_type", "base_url", "wire_api", "model_name", "reasoning_effort", "api_key", "enabled", "roles",
  "action_protocol", "structured_output_mode", "request_timeout_seconds", "max_output_tokens",
  "temperature", "max_retries", "retry_base_seconds", "rate_limit_cooldown_seconds",
  "requests_per_minute", "max_concurrency", "context_token_limit",
  // ``route_via_proxy`` is a real boolean and must survive a ``false`` (it means
  // "turn the proxy off", not "leave as is").  ``model_aliases_text`` is a UI
  // convenience field and is converted below, never sent verbatim.
  "route_via_proxy",
];

//: The official Claude names the CLI may send, offered as the default alias rows.
//: Mirrors _OFFICIAL_CLAUDE_MODELS in app/api/claude_proxy.py.
const OFFICIAL_CLAUDE_ALIAS_KEYS = [
  "claude-opus-5",
  "claude-sonnet-5",
  "claude-haiku-4-5",
  "claude-3-5-sonnet-latest",
  "claude-3-5-haiku-latest",
];

/** ``{official: upstream}`` -> one ``official=upstream`` line per entry. */
function aliasesToText(aliases?: Record<string, string> | null): string {
  if (!aliases) return "";
  return Object.entries(aliases).map(([name, model]) => `${name}=${model}`).join("\n");
}

/** One ``official=upstream`` line per entry -> a map; blank lines are ignored. */
function textToAliases(text?: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of String(text ?? "").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const at = trimmed.indexOf("=");
    if (at <= 0) continue;
    const name = trimmed.slice(0, at).trim();
    const model = trimmed.slice(at + 1).trim();
    if (name && model) out[name] = model;
  }
  return out;
}

const _NUMBER_FIELDS: Record<string, true> = {
  request_timeout_seconds: true, max_output_tokens: true, max_retries: true,
  rate_limit_cooldown_seconds: true, requests_per_minute: true, max_concurrency: true,
  context_token_limit: true, retry_base_seconds: true,
};
const _FLOAT_FIELDS: Record<string, true> = { temperature: true };

function toModelConfigPayload(values: ModelConfigFormValues): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of modelConfigFields) {
    const raw = values[field];
    if (raw === undefined || raw === "") continue;
    if (_NUMBER_FIELDS[field]) {
      payload[field] = Number(raw);
    } else if (_FLOAT_FIELDS[field]) {
      payload[field] = parseFloat(raw as string);
    } else {
      payload[field] = raw;
    }
  }
  // Select mode="tags" returns string[]; model_name must be a plain string.
  if (Array.isArray(payload.model_name)) {
    payload.model_name = (payload.model_name as string[])[0] ?? "";
  }
  // The alias editor is free text; the API wants a map.  Only send the key when
  // this config is a claude_cli with the proxy on -- an empty map would otherwise
  // clear aliases a non-proxy config has no use for, and codex takes no aliases
  // at all (it sends the real upstream name, unlike the Claude CLI).
  if (values.provider_type === "claude_cli" && values.route_via_proxy) {
    payload.model_aliases = textToAliases(values.model_aliases_text);
  }
  return payload;
}

function StatusTag({ reachable }: { reachable?: boolean }) {
  return <Tag color={reachable ? "success" : "error"}>{reachable ? "服务正常" : "不可达"}</Tag>;
}

function roleLabel(role: string) {
  return role === "coordinator_reason" ? "Coordinator Reason" : "Worker";
}

type RoleKey = "coordinator_reason" | "worker";
type ProviderKey = ModelProviderType;

// Order of the provider sub-cards inside every role section. Mirrors
// ProviderType in app/schemas/model_config.py.
//
// cursor_cli is deliberately absent: the channel is retired (see
// RETIRED_PROVIDER_TYPES on the backend). Its configurations already in the
// database still render in the list -- only *choosing* it is gone.
const PROVIDER_ORDER: ProviderKey[] = [
  "openai_compatible",
  "local_proxy",
  "codex_cli",
  "claude_cli",
];

// Providers backed by a shelled CLI agent. Mirrors CLI_PROVIDER_TYPES: they
// execute tools, so they can never carry the read-only Reason role.
const CLI_PROVIDER_KEYS = new Set<ProviderKey>(["codex_cli", "claude_cli"]);
// Providers that speak the OpenAI Chat Completions wire protocol, and therefore
// require base_url + api_key. Mirrors OPENAI_WIRE_PROVIDER_TYPES.
const OPENAI_WIRE_PROVIDER_KEYS = new Set<ProviderKey>(["openai_compatible", "local_proxy"]);
// CLI providers that may pin an API endpoint instead of the host's own
// subscription login. Mirrors ENDPOINT_CAPABLE_CLI_PROVIDERS.
const ENDPOINT_CAPABLE_PROVIDER_KEYS = new Set<ProviderKey>(["codex_cli", "claude_cli"]);
// Providers whose base_url must be loopback. Mirrors app/core/net.py: a local
// proxy shim has no authentication of its own.
const LOOPBACK_PROVIDER_KEYS = new Set<ProviderKey>(["local_proxy"]);

// Client-side pre-check for the loopback contract. The backend stays
// authoritative (app.core.net.require_loopback_url); this only turns a rejected
// save into an inline field error. Names are deliberately NOT resolved, exactly
// like the backend -- DNS would open a rebinding window between validation and use.
//
// Two asymmetries with the backend are accepted, both in the safe direction
// (this check may accept what the backend then refuses, never the reverse):
// the URL parser normalizes shorthand IPv4 forms such as `127.1` or `0x7f.1`
// to `127.0.0.1` before we see them, while Python's ip_address() rejects the
// shorthand spellings outright.
const LOOPBACK_NAMES = new Set(["localhost"]);
const IPV4_OCTET = /^\d{1,3}$/;
function isLoopbackUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  const host = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (LOOPBACK_NAMES.has(host)) return true;
  // 127.0.0.0/8 and ::1 only. A private-but-not-loopback range is rejected on
  // purpose: the requirement is loopback, not "on my LAN".
  if (host === "::1") return true;
  // The whole host must be a four-octet literal in 127/8. A prefix test would
  // accept `127.0.0.1.evil.com`, which is a name, not loopback.
  const octets = host.split(".");
  return (
    octets.length === 4 &&
    octets[0] === "127" &&
    octets.every((part) => IPV4_OCTET.test(part) && Number(part) <= 255)
  );
}

// Two-level classification: Muteki role first, provider second. Both word lists
// mirror the runtime vocabulary so a section maps onto exactly one dispatch
// path (hints_json.muteki_runtime.reason_model_config_id / worker_engines).
const ROLE_SECTIONS: Array<{ key: RoleKey; label: string; hint: string }> = [
  {
    key: "coordinator_reason",
    label: "Coordinator Reason",
    hint: "只读取 Blackboard 并产出受策略约束的 Intent，不直接执行工具。Codex / Claude / Cursor CLI 的 provider 合同限定为 Worker 角色，因此不在本分类列出。",
  },
  {
    key: "worker",
    label: "Worker 引擎",
    hint: "执行 Intent；创建任务时可勾选多个引擎并行调度。本地反代没有独立引擎，创建任务时归入 openai_compatible。",
  },
];

// Labels for the provider level of the split; the CLI ones document the
// transport they own, codex_cli's the fact that it only speaks Responses.
//
// These three maps stay COMPLETE even though cursor_cli is retired: a legacy row
// still has to render its name, icon and model hint in the list. Retirement only
// removes cursor_cli from PROVIDER_ORDER, i.e. from what can be chosen.
const PROVIDER_LABELS: Record<ProviderKey, string> = {
  openai_compatible: "OpenAI-compatible（API）",
  local_proxy: "本地反代（仅回环）",
  codex_cli: "Codex CLI / API（Responses）",
  claude_cli: "Claude CLI / API",
  cursor_cli: "Cursor CLI（已停用）",
};

// A role tab only offers the providers its role can legally carry: the CLI
// providers are rejected for coordinator_reason by the backend contract.
const ROLE_PROVIDERS: Record<RoleKey, ProviderKey[]> = {
  coordinator_reason: ["openai_compatible", "local_proxy"],
  worker: PROVIDER_ORDER,
};

const PROVIDER_ICONS: Record<ProviderKey, ReactNode> = {
  openai_compatible: <ApiOutlined />,
  local_proxy: <CloudServerOutlined />,
  codex_cli: <ToolOutlined />,
  claude_cli: <RobotOutlined />,
  cursor_cli: <CodeOutlined />,
};

const MODEL_NAME_PLACEHOLDERS: Record<ProviderKey, string> = {
  openai_compatible: "step-xxx / deepseek-chat",
  local_proxy: "deepseek-chat（反代暴露的模型名）",
  codex_cli: "gpt-5.6-sol",
  claude_cli: "claude-sonnet-4-5",
  cursor_cli: "cursor 默认模型",
};

function rolesOf(item: ModelConfig): RoleKey[] {
  const roles = (item.roles ?? []).filter(
    (role): role is RoleKey => role === "worker" || role === "coordinator_reason",
  );
  return roles.length ? roles : ["worker"];
}

// One config may carry both roles, so it is listed in both role sections.
function groupByRoleAndProvider(items: ModelConfig[]) {
  const grouped = {} as Record<RoleKey, Record<ProviderKey, ModelConfig[]>>;
  for (const role of ROLE_SECTIONS) {
    const byProvider = {} as Record<ProviderKey, ModelConfig[]>;
    for (const key of PROVIDER_ORDER) byProvider[key] = [];
    grouped[role.key] = byProvider;
  }
  const known = new Set<string>(PROVIDER_ORDER);
  for (const item of items) {
    // An unrecognized provider_type (a newer backend) is filed under the OpenAI
    // wire protocol rather than dropped off the page silently.
    const provider: ProviderKey = known.has(item.provider_type)
      ? (item.provider_type as ProviderKey)
      : "openai_compatible";
    for (const role of rolesOf(item)) grouped[role][provider].push(item);
  }
  return grouped;
}

export function SettingsPage() {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ModelConfig>();
  const [form] = Form.useForm();
  const [serviceOpen, setServiceOpen] = useState(false);
  const [serviceForm] = Form.useForm<ServiceForm>();
  const [bindingConfig, setBindingConfig] = useState<ModelConfig>();
  const [bindingValues, setBindingValues] = useState<SkillBinding[]>([]);
  const [removing, setRemoving] = useState<ModelConfig>();
  // One row per config the operator asked to probe in bulk.  The single
  // "测试连接" button answers "is this one alive"; when picking an upstream
  // among a dozen relays the actual question is "which of these still work",
  // and doing that one click at a time is what this state exists to avoid.
  const [probeRows, setProbeRows] = useState<Array<{ id: string; name: string; provider: string; model: string }>>([]);
  const [probing, setProbing] = useState(false);
  const [probeResults, setProbeResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const configs = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  const services = useQuery({ queryKey: ["system-settings"], queryFn: api.getSystemSettings, refetchInterval: 15_000 });
  const removalUsage = useQuery({
    queryKey: ["model-config-usage", removing?.id],
    queryFn: () => api.getModelConfigUsage(removing!.id),
    enabled: Boolean(removing),
  });
  const refresh = () => void client.invalidateQueries({ queryKey: ["model-configs"] });
  const refreshServices = () => void client.invalidateQueries({ queryKey: ["system-settings"] });
  const grouped = groupByRoleAndProvider(configs.data ?? []);
  // Only enabled configs are offered as a default: the backend refuses a
  // disabled one, so listing it would only produce a 422 on save.
  const enabledConfigs = (configs.data ?? []).filter((item) => item.enabled);

  const save = useMutation({
    mutationFn: (values: ModelConfigFormValues) => {
      const payload = toModelConfigPayload(values);
      return editing ? api.updateModelConfig(editing.id, payload) : api.createModelConfig(payload);
    },
    onSuccess: () => {
      message.success("模型配置已保存");
      setOpen(false);
      setEditing(undefined);
      form.resetFields();
      refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteModelConfig(id),
    onSuccess: () => {
      message.success("模型配置已删除");
      setRemoving(undefined);
      refresh();
    },
    // The backend answers 409 MODEL_CONFIG_IN_USE when a foreign key still
    // refuses the delete; that message names the blocking tables, so surface it
    // verbatim instead of a generic failure.
    onError: (error: Error) => message.error(error.message),
  });
  const test = useMutation({
    mutationFn: api.testModelConfig,
    onSuccess: (result) => result.ok ? message.success(result.message) : message.error(result.message),
    onError: (error: Error) => message.error(error.message),
  });
  // Serial on purpose.  Every probe is a real request to a real relay, and
  // firing sixteen at once turns a quota question into a rate-limit answer:
  // the ones that fail would fail for our own reason, not theirs.
  const probeAll = async () => {
    const rows = (configs.data ?? []).map((item) => ({
      id: item.id,
      name: item.name,
      provider: item.provider_type,
      model: item.model_name ?? "",
    }));
    setProbeRows(rows);
    setProbeResults({});
    setProbing(true);
    try {
      for (const row of rows) {
        // Mark the row as "in flight" so a long relay does not look frozen.
        setProbeResults((previous) => ({ ...previous, [row.id]: { ok: false, message: "正在测试…" } }));
        try {
          const result = await api.testModelConfig(row.id);
          setProbeResults((previous) => ({ ...previous, [row.id]: { ok: result.ok, message: result.message } }));
        } catch (error) {
          setProbeResults((previous) => ({
            ...previous,
            [row.id]: { ok: false, message: (error as Error).message },
          }));
        }
      }
    } finally {
      setProbing(false);
      refresh();
    }
  };
  const saveServices = useMutation({
    mutationFn: api.updateSystemSettings,
    onSuccess: () => { message.success("服务设置已保存"); setServiceOpen(false); refreshServices(); },
    onError: (error: Error) => message.error(error.message),
  });
  const saveBindings = useMutation({
    mutationFn: async () => api.setModelSkills(bindingConfig!.id, bindingValues),
    onSuccess: () => { message.success("Skills 绑定已保存"); setBindingConfig(undefined); },
    onError: (error: Error) => message.error(error.message),
  });

  const edit = (item?: ModelConfig, role: RoleKey = "worker", provider: ProviderKey = "openai_compatible") => {
    setEditing(item);
    form.resetFields();
    form.setFieldsValue(item ? {
      name: item.name,
      provider_type: item.provider_type,
      base_url: item.base_url,
      wire_api: item.wire_api ?? undefined,
      model_name: item.model_name,
      reasoning_effort: item.reasoning_effort ?? "medium",
      api_key: undefined,
      enabled: item.enabled,
      roles: item.roles ?? ["worker"],
      action_protocol: item.action_protocol,
      structured_output_mode: item.structured_output_mode,
      request_timeout_seconds: item.request_timeout_seconds,
      max_output_tokens: item.max_output_tokens,
      temperature: item.temperature,
      max_retries: item.max_retries,
      retry_base_seconds: item.retry_base_seconds,
      rate_limit_cooldown_seconds: item.rate_limit_cooldown_seconds,
      requests_per_minute: item.requests_per_minute,
      max_concurrency: item.max_concurrency,
      context_token_limit: item.context_token_limit,
      route_via_proxy: item.route_via_proxy ?? false,
      model_aliases_text: aliasesToText(item.model_aliases),
    } : { provider_type: provider, enabled: true, roles: [role] });
    setOpen(true);
  };
  const editServices = () => {
    serviceForm.resetFields();
    if (services.data) serviceForm.setFieldsValue({
      // Only seeded when the Runner is actually part of the deployment; the
      // backend keeps the stored value when the field is absent, so omitting it
      // cannot wipe the address of a Runner someone still fronts.
      ...(services.data.legacy_kali_runner ? { runner_url: services.data.runner_url } : {}),
      default_model_config_id: services.data.default_model_config_id ?? undefined,
    });
    setServiceOpen(true);
  };
  const editBindings = async (item: ModelConfig) => {
    setBindingConfig(item);
    setBindingValues(await api.getModelSkills(item.id) as SkillBinding[]);
  };

  // Role and provider are owned by the enclosing section, so they are no longer
  // repeated as columns; only a config that carries both roles is flagged.
  const configColumns = [
    {
      title: "名称",
      dataIndex: "name",
      width: 200,
      render: (value: string, item: ModelConfig) => (
        rolesOf(item).length > 1
          ? <Space size={4}><span>{value}</span><Tag color="orange">Reason / Worker 共用</Tag></Space>
          : value
      ),
    },
    { title: "模型", dataIndex: "model_name", width: 180, render: (value: string, item: ModelConfig) => <Space direction="vertical" size={0}><span>{value || "—"}</span>{item.provider_type === "codex_cli" ? <Tag color="purple">effort: {item.reasoning_effort ?? "medium"}</Tag> : null}</Space> },
    { title: "Base URL", dataIndex: "base_url", width: 260, ellipsis: true },
    { title: "API Key", dataIndex: "api_key_configured", width: 100, render: (value: boolean) => value ? <Tag color="success">已配置</Tag> : <Tag>未配置</Tag> },
    { title: "状态", dataIndex: "enabled", width: 90, render: (value: boolean) => <Tag color={value ? "success" : "default"}>{value ? "启用" : "禁用"}</Tag> },
    {
      title: "操作",
      width: 300,
      fixed: "right" as const,
      render: (_: unknown, item: ModelConfig) => (
        <Space size={0} wrap>
          <Button type="link" onClick={() => edit(item)}>编辑</Button>
          <Button type="link" onClick={() => void editBindings(item)}>配置 Skills</Button>
          <Button
            type="link"
            loading={test.isPending && test.variables === item.id}
            disabled={test.isPending}
            onClick={() => test.mutate(item.id)}
          >
            {test.isPending && test.variables === item.id ? "正在测试连接…" : "测试连接"}
          </Button>
          {test.isPending && test.variables === item.id ? (
            <span style={{ color: "#8c8c8c", fontSize: 12 }}>
              正在测试连接...
            </span>
          ) : null}
          <Button type="link" danger onClick={() => setRemoving(item)}>删除</Button>
        </Space>
      ),
    },
  ];

  return <>
    <div className="page-heading">
      <div><h1>系统配置</h1><p>配置 Coordinator Reason、Worker 引擎和执行服务。</p></div>
      <Space>
        <Button onClick={() => void probeAll()} loading={probing}>批量测试全部（{configs.data?.length ?? 0}）</Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => edit()}>新建模型配置</Button>
      </Space>
    </div>
    <Alert className="panel-card" type="warning" showIcon icon={<KeyOutlined />} message="API Key 只写入后端，前端仅显示是否已配置。" />

    {probeRows.length ? (
      <Card
        className="panel-card"
        title={`批量测试结果（${probeRows.filter((row) => probeResults[row.id]?.message === "模型能力测试完成").length} 可用 / ${probeRows.length} 总数）`}
        style={{ marginTop: 18 }}
        extra={<Space><span style={{ color: "#8c8c8c", fontSize: 12 }}>逐条串行执行，避免把自己的限流当成上游故障</span><Button size="small" onClick={() => { setProbeRows([]); setProbeResults({}); }} disabled={probing}>收起</Button></Space>}
      >
        <Table
          className="cyber-table"
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={probeRows}
          tableLayout="fixed"
          scroll={{ x: 900 }}
          columns={[
            { title: "名称", dataIndex: "name", width: 190, ellipsis: true },
            { title: "提供方", dataIndex: "provider", width: 150 },
            { title: "模型", dataIndex: "model", width: 180, ellipsis: true },
            {
              title: "结果",
              width: 120,
              render: (_: unknown, row: { id: string }) => {
                const result = probeResults[row.id];
                if (!result) return <Tag>待测</Tag>;
                if (result.message === "正在测试…") return <Tag color="processing">测试中</Tag>;
                return result.ok ? <Tag color="success">可用</Tag> : <Tag color="error">不可用</Tag>;
              },
            },
            {
              title: "说明",
              render: (_: unknown, row: { id: string }) => {
                const result = probeResults[row.id];
                if (!result || result.message === "正在测试…") return <span style={{ color: "#8c8c8c" }}>—</span>;
                return <span style={{ color: result.ok ? "#14b8a6" : "#fb7185", fontSize: 12, wordBreak: "break-all" }}>{result.message}</span>;
              },
            },
          ]}
        />
      </Card>
    ) : null}

    <Card
      className="panel-card"
      title="模型配置"
      style={{ marginTop: 18 }}
      extra={<span style={{ color: "#8c8c8c", fontSize: 12 }}>按「Muteki 角色 → 提供方」两级分开存放</span>}
    >
      <Tabs
        items={ROLE_SECTIONS.map((role) => {
          const byProvider = grouped[role.key];
          const total = ROLE_PROVIDERS[role.key].reduce((sum, key) => sum + byProvider[key].length, 0);
          return {
            key: role.key,
            label: `${role.label}（${total}）`,
            children: (
              <>
                <Alert type="info" showIcon message={role.hint} style={{ marginBottom: 14 }} />
                {ROLE_PROVIDERS[role.key].map((providerKey) => {
                  const rows = byProvider[providerKey];
                  return (
                    <Card
                      key={providerKey}
                      size="small"
                      style={{ marginBottom: 14 }}
                      title={<Space>{PROVIDER_ICONS[providerKey]}{PROVIDER_LABELS[providerKey]}<Tag>{rows.length}</Tag></Space>}
                      extra={<Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => edit(undefined, role.key, providerKey)}>新建</Button>}
                    >
                      {providerKey === "local_proxy" ? (
                        <Alert
                          type="warning"
                          showIcon
                          style={{ marginBottom: 12 }}
                          message="本地反代仅接受回环地址（127.0.0.0/8、::1 或 localhost）。私网地址（192.168.x.x、10.x.x.x）会被拒绝：该代理自身没有认证，暴露到网络即成为外泄通道。"
                        />
                      ) : null}
                      <Table
                        className="cyber-table"
                        rowKey="id"
                        size="small"
                        dataSource={rows}
                        loading={configs.isLoading}
                        scroll={{ x: 1130 }}
                        tableLayout="fixed"
                        locale={{ emptyText: <Empty description={`暂无 ${PROVIDER_LABELS[providerKey]} 配置`} image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                        columns={configColumns}
                      />
                    </Card>
                  );
                })}
              </>
            ),
          };
        })}
      />
    </Card>

    <Card className="panel-card" title="Muteki Runtime 分工" style={{ marginTop: 18 }}>
      <Alert type="info" showIcon message="Coordinator Reason 只读取 Blackboard 并生成 Intent；Worker 引擎负责执行 Intent。创建任务时可以选择多个 Worker 并行调度。" />
      <Space wrap style={{ marginTop: 14 }}>{(configs.data ?? []).map((item) => <Tag key={item.id} color={item.enabled ? "green" : "default"}>{item.name} · {(item.roles ?? ["worker"]).map(roleLabel).join(" / ")}</Tag>)}</Space>
    </Card>

    <Card className="panel-card" title="执行服务" extra={<Space><Button onClick={refreshServices}>刷新状态</Button><Button type="primary" onClick={editServices}>编辑服务设置</Button></Space>} style={{ marginTop: 18 }}>
      {services.isLoading ? <Spin /> : <Descriptions column={{ xs: 1, md: 2 }} items={[
        // The legacy Kali Runner rows are rendered only when the deployment
        // still fronts one. Showing them unconditionally meant a permanently red
        // "unreachable" beside an address nothing in the solve path uses.
        ...(services.data?.legacy_kali_runner === false
          ? [{
              key: "tool-channel",
              label: "工具执行通道",
              children: <Tag color="success">容器原生 Worker（Muteki）</Tag>,
              span: 2,
            }]
          : [
              { key: "runner-url", label: "Kali Runner 地址", children: services.data?.runner_url ?? "—" },
              { key: "runner-state", label: "Runner 状态", children: <StatusTag reachable={services.data?.runner.reachable} /> },
              { key: "runner-cidr", label: "Runner 允许网段", children: services.data?.runner_allowed_cidrs ?? "—" },
              { key: "runner-token", label: "Runner Token", children: services.data?.runner_token_configured ? <Tag color="success">已配置</Tag> : <Tag color="error">未配置</Tag> },
            ]),
        {
          key: "default-model",
          label: "全局默认模型",
          children: services.data?.default_model_label
            ? <Tag color="blue">{services.data.default_model_label}</Tag>
            : <span style={{ color: "#8c8c8c" }}>未设置（未单独指定模型的连接将无法开始竞技场求解）</span>,
        },
      ]} />}
    </Card>

    <Tabs className="panel-card" style={{ marginTop: 18 }} items={[
      { key: "reason", label: "Coordinator Reason", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "source", label: "职责", children: "读取 Blackboard，输出受策略约束的 Intent。" }, { key: "fallback", label: "失败回退", children: "使用 Muteki 本地策略规划器，不会让 Reason 直接执行工具。" }, { key: "output", label: "输出", children: "verdict + typed intents，不包含 Worker 原始响应。" }]} /> },
      { key: "worker", label: "Worker Engines", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "parallel", label: "并行", children: "按创建任务时勾选的 EngineProfile 调度。" }, { key: "supported", label: "可选引擎", children: "Codex CLI / Claude CLI / Cursor CLI / OpenAI-compatible（含本地反代）。" }, { key: "boundary", label: "执行边界", children: "CLI 引擎走各自官方 Worker/Sandbox；兼容模型与本地反代通过受限 Worker 适配器进入 Tool Gateway。" }]} /> },
      { key: "tools", label: "Tool Policy", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "allow", label: "工具边界", children: "题型、Worker 角色和 Runner Policy 共同约束。" }, { key: "risk", label: "风险等级", children: "由现有安全层和 Tool Gateway 继续控制。" }]} /> },
      { key: "audit", label: "数据与审计", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "redact", label: "敏感字段脱敏", children: "启用。" }, { key: "summary", label: "模型响应", children: "只保留安全摘要和 Token 用量，不保存原始响应。" }]} /> },
    ]} />

    <Row gutter={[18, 18]} style={{ marginTop: 18 }}>
      <Col xs={24} md={6}><Card className="panel-card" title={<><ApiOutlined /> OpenAI-compatible</>}>可配置 Step、DeepSeek 等兼容 Chat Completions 的模型，并分别指定 Coordinator Reason 或 Worker 角色。</Card></Col>
      <Col xs={24} md={6}><Card className="panel-card" title={<><ToolOutlined /> Codex CLI / API</>}>通过 Muteki 官方 Worker/Sandbox 适配器执行；支持本地订阅或配置 Responses API 端点。</Card></Col>
      <Col xs={24} md={6}><Card className="panel-card" title={<><RobotOutlined /> Claude / Cursor CLI</>}>复用宿主机的 Claude / Cursor 登录态执行；Claude 可另接 API 端点，两者均只作为 Worker。</Card></Col>
      <Col xs={24} md={6}><Card className="panel-card" title={<><SafetyCertificateOutlined /> Security Boundary</>}>本地反代仅接受 127.0.0.0/8、::1 或 localhost（代理自身没有认证）；模型只能生成受约束的 Intent，工具执行仍经过安全层与 Tool Gateway。</Card></Col>
    </Row>

    <Modal open={open} title={editing ? "编辑模型配置" : "新建模型配置"} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending}>
      <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values as ModelConfigFormValues)}>
        <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="provider_type" label="提供方" rules={[{ required: true }]}>
          <Select
            options={PROVIDER_ORDER.map((key) => ({ value: key, label: PROVIDER_LABELS[key] }))}
            onChange={(value: ProviderKey) => {
              const patch: Partial<ModelConfigFormValues> = {};
              // A CLI provider executes tools, so it can only ever be a Worker.
              if (CLI_PROVIDER_KEYS.has(value)) patch.roles = ["worker"];
              // Every CLI provider can carry an endpoint now, so only a provider
              // that cannot hold one at all drops an inherited base_url/api_key.
              if (!OPENAI_WIRE_PROVIDER_KEYS.has(value) && !ENDPOINT_CAPABLE_PROVIDER_KEYS.has(value)) {
                patch.base_url = undefined;
                patch.api_key = undefined;
              }
              // Only a CLI provider has no wire dialect to hold; the OpenAI-wire
              // providers keep theirs (and the backend defaults it anyway).
              if (!OPENAI_WIRE_PROVIDER_KEYS.has(value) && value !== "codex_cli") {
                patch.wire_api = undefined;
              }
              if (OPENAI_WIRE_PROVIDER_KEYS.has(value)) patch.wire_api = "chat_completions";
              if (value === "codex_cli") patch.reasoning_effort = "medium";
              form.setFieldsValue(patch);
            }}
          />
        </Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type}>{({ getFieldValue }) => {
          const provider: ProviderKey = getFieldValue("provider_type");
          // OpenAI wire protocol (incl. the loopback-only local proxy): the
          // endpoint IS the provider, so it is mandatory.
          if (OPENAI_WIRE_PROVIDER_KEYS.has(provider)) {
            const loopback = LOOPBACK_PROVIDER_KEYS.has(provider);
            return (
              <Form.Item
                name="base_url"
                label="Base URL"
                rules={[
                  { required: true, type: "url" },
                  {
                    // Mirrors app.core.net.require_loopback_url; the backend
                    // stays authoritative, this only moves the rejection inline.
                    validator: (_rule: unknown, value: unknown) =>
                      !loopback || isLoopbackUrl(String(value ?? ""))
                        ? Promise.resolve()
                        : Promise.reject(new Error("必须是回环地址：127.0.0.0/8、::1 或 localhost")),
                  },
                ]}
                extra={
                  loopback
                    ? "该代理自身没有认证，只接受本机回环地址"
                    : "填根地址（https://api.example.com/v1）即可；直接粘贴完整端点（.../chat/completions）也可以，不会被重复拼接"
                }
              >
                <Input placeholder="https://api.example.com/v1" />
              </Form.Item>
            );
          }
          // CLI engines that can pin an API endpoint instead of the host login.
          // Every offered CLI provider qualifies, so this branch is the fallback.
          return (
            <Form.Item
              name="base_url"
              label="Base URL（可选，API 模式必填）"
              rules={[{ type: "url" }]}
              extra={
                provider === "claude_cli"
                  ? "留空时使用本机 Claude 登录态；可填 https://api.anthropic.com 或 .../v1/messages"
                  : "留空时使用本机/容器登录态；填写后走该 CLI 自己的端点配置"
              }
            >
              <Input placeholder="https://api.anthropic.com" />
            </Form.Item>
          );
        }}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type || previous.base_url !== current.base_url}>{({ getFieldValue }) => {
          const provider = getFieldValue("provider_type");
          const baseUrl = getFieldValue("base_url");
          if (provider === "codex_cli" && baseUrl) {
            return <Form.Item name="wire_api" label="Wire API 协议" initialValue="responses" rules={[{ required: true }]} extra="codex 仅支持 Responses API：chat 已被上游移除、chat_completions 无法被 codex 加载，因此固定为 responses"><Select disabled options={[{ value: "responses", label: "responses（codex 唯一支持）" }]} /></Form.Item>;
          }
          // An OpenAI-wire provider may serve either dialect.  A relay that only
          // exposes /chat/completions answers /responses with a 502, and one that
          // only exposes /responses answers /chat/completions with a 404 -- both
          // look like an outage, so the choice has to be explicit.
          if (OPENAI_WIRE_PROVIDER_KEYS.has(provider)) {
            return <Form.Item name="wire_api" label="Wire API 协议" initialValue="chat_completions" rules={[{ required: true }]} extra="按站点实际支持的协议选：只提供 /chat/completions 就选 chat_completions；只提供 /responses 就选 responses"><Select options={[{ value: "chat_completions", label: "chat_completions（/chat/completions，多数中转）" }, { value: "responses", label: "responses（/responses）" }]} /></Form.Item>;
          }
          return null;
        }}</Form.Item>
        {/* Routing controls for a CLI Worker.  Shown for both CLI providers:
            each owns a wire protocol that a chat-only relay refuses, and each
            has its own local translating proxy. */}
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type || previous.base_url !== current.base_url}>{({ getFieldValue }) => {
          const provider = getFieldValue("provider_type");
          const baseUrl = getFieldValue("base_url");
          if (provider !== "claude_cli" && provider !== "codex_cli") return null;
          const isClaude = provider === "claude_cli";
          // A codex_cli with no base_url runs on the host's own subscription
          // login, so there is no endpoint to translate and the runtime never
          // consults the proxy.  Offering the switch there would be a control
          // that does nothing.
          if (!isClaude && !baseUrl) return null;
          return (
            <div style={{ marginBottom: 16, padding: 12, border: "1px solid #f0f0f0", borderRadius: 8 }}>
              <Form.Item
                name="route_via_proxy"
                label="经本地代理路由（route_via_proxy）"
                valuePropName="checked"
                style={{ marginBottom: 8 }}
                extra={isClaude
                  ? "上游只放行 /chat/completions 时必开：Worker 走本机协议转换代理，由它把 Anthropic Messages 翻译成 OpenAI chat。多数中转分组会拒绝 /v1/messages（提示 does not allow /v1/messages dispatch），此时不开必失败。"
                  : "上游只放行 /chat/completions 时必开：codex CLI 只讲 Responses，本机代理把 Responses 翻译成 OpenAI chat 再转发。多数中转会拒绝 /responses（提示「本站不提供 Responses(reasoning) 协议」），此时不开必失败。"}
              >
                <Switch />
              </Form.Item>
              <Form.Item noStyle shouldUpdate={(previous, current) => previous.route_via_proxy !== current.route_via_proxy}>{({ getFieldValue: get }) => get("route_via_proxy") && isClaude ? (
                <Form.Item
                  name="model_aliases_text"
                  label="模型别名（每行一条：官方名=上游模型名）"
                  extra={`Claude CLI 只会发送它内置的官方名，代理靠这张表把它翻译成上游模型名，同时用它决定请求该走哪条配置。可留空使用默认：${OFFICIAL_CLAUDE_ALIAS_KEYS.slice(0, 3).join("、")}…`}
                >
                  <Input.TextArea rows={5} placeholder={OFFICIAL_CLAUDE_ALIAS_KEYS.map((k) => `${k}=你的上游模型名`).join("\n")} />
                </Form.Item>
              ) : null}</Form.Item>
            </div>
          );
        }}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type}>{({ getFieldValue }) => getFieldValue("provider_type") === "codex_cli" ? <Collapse style={{ marginBottom: 16 }} items={[{ key: "codex-profile", label: "Codex CLI Worker 参数（点击展开选择）", children: <><Form.Item name="model_name" label="模型名称" rules={[{ required: true }]}><Select mode="tags" maxCount={1} placeholder="输入或选择模型名称" showSearch filterOption={((input, option) => (option?.label ?? "").toLowerCase().includes(input.toLowerCase()))} options={["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"].map((value) => ({ value, label: value }))} /></Form.Item><Form.Item name="reasoning_effort" label="推理强度" rules={[{ required: true }]} extra="只选择强度，不要拼接到模型名；实际传给 Codex CLI 的是 model_reasoning_effort。"><Select options={[{ value: "none", label: "none（关闭）" }, { value: "low", label: "low" }, { value: "medium", label: "medium（默认）" }, { value: "high", label: "high（推荐）" }, { value: "xhigh", label: "xhigh" }, { value: "max", label: "max" }, { value: "ultra", label: "ultra（若本机 CLI 支持）" }]} /></Form.Item></> }]} /> : <Form.Item name="model_name" label="模型名称" rules={[{ required: true }]}><Input placeholder={MODEL_NAME_PLACEHOLDERS[getFieldValue("provider_type") as ProviderKey] ?? "step-xxx / deepseek-chat"} /></Form.Item>}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type || previous.base_url !== current.base_url}>{({ getFieldValue }) => {
          const provider: ProviderKey = getFieldValue("provider_type");
          const baseUrl = getFieldValue("base_url");
          // Mirrors _stores_api_key() in app/api/v1/models.py: the OpenAI-wire
          // providers always carry a key (the backend rejects a blank one), and
          // an endpoint-pinned CLI provider needs one to authenticate. A CLI
          // running on the host's own subscription login needs none.
          if (OPENAI_WIRE_PROVIDER_KEYS.has(provider) || (ENDPOINT_CAPABLE_PROVIDER_KEYS.has(provider) && baseUrl)) {
            return (
              <Form.Item
                name="api_key"
                label={editing ? "新 API Key（留空则不变）" : "API Key"}
                rules={editing ? [] : [{ required: true }]}
                extra={LOOPBACK_PROVIDER_KEYS.has(provider) ? "后端要求非空；反代若不校验认证可填占位值" : undefined}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            );
          }
          return null;
        }}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type}>{({ getFieldValue }) => {
          // CLI engines are Worker-only by provider contract, so the role list
          // collapses to the single legal value instead of offering an option
          // the backend would reject.
          const workerOnly = CLI_PROVIDER_KEYS.has(getFieldValue("provider_type"));
          return (
            <Form.Item name="roles" label="Muteki 角色" rules={[{ required: true }]}>
              <Select
                mode="multiple"
                disabled={workerOnly}
                options={workerOnly
                  ? [{ value: "worker", label: "Worker（执行引擎）" }]
                  : [{ value: "coordinator_reason", label: "Coordinator Reason（协调器推理）" }, { value: "worker", label: "Worker（执行引擎）" }]}
              />
            </Form.Item>
          );
        }}</Form.Item>
        <Form.Item name="action_protocol" label="Action Protocol"><Input placeholder="json_schema / json_object / prompt_json" /></Form.Item>
        <Form.Item name="structured_output_mode" label="Structured Output Mode"><Input placeholder="json_schema / json_object / prompt_json" /></Form.Item>
        <Row gutter={12}><Col span={12}><Form.Item name="request_timeout_seconds" label="Timeout"><Input type="number" /></Form.Item></Col><Col span={12}><Form.Item name="max_output_tokens" label="Max Output Tokens"><Input type="number" /></Form.Item></Col></Row>
        <Row gutter={12}><Col span={12}><Form.Item name="temperature" label="Temperature"><Input type="number" step="0.1" /></Form.Item></Col><Col span={12}><Form.Item name="context_token_limit" label="Context Token Limit"><Input type="number" /></Form.Item></Col></Row>
        <Row gutter={12}><Col span={12}><Form.Item name="max_retries" label="Max Retries"><Input type="number" /></Form.Item></Col><Col span={12}><Form.Item name="retry_base_seconds" label="Retry Backoff"><Input type="number" /></Form.Item></Col></Row>
        <Row gutter={12}><Col span={12}><Form.Item name="rate_limit_cooldown_seconds" label="429 Cooldown"><Input type="number" /></Form.Item></Col><Col span={12}><Form.Item name="requests_per_minute" label="RPM"><Input type="number" /></Form.Item></Col></Row>
        <Form.Item name="max_concurrency" label="Max Concurrency"><Input type="number" /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>

    <Modal
      open={Boolean(removing)}
      title={`删除模型配置：${removing?.name ?? ""}`}
      okText="删除"
      okButtonProps={{ danger: true, loading: remove.isPending }}
      cancelText="取消"
      onCancel={() => setRemoving(undefined)}
      onOk={() => removing && remove.mutate(removing.id)}
    >
      <p style={{ marginTop: 0 }}>删除后无法恢复，请确认影响范围。</p>
      {removalUsage.isLoading ? (
        <Spin />
      ) : removalUsage.data ? (
        <Descriptions column={1} size="small" bordered items={[
          {
            key: "refs",
            label: "引用情况",
            children: Object.values(removalUsage.data.references).some((count) => count > 0)
              ? Object.entries(removalUsage.data.references)
                  .filter(([, count]) => count > 0)
                  .map(([table, count]) => `${table} × ${count}`)
                  .join("、")
              : "没有任何引用",
          },
          {
            key: "effect",
            label: "对历史的影响",
            children: "任务与 run 历史会完整保留，仅解除与本次配置的关联（模型名称可能不再显示）。",
          },
        ]} />
      ) : null}
    </Modal>

    <Modal open={serviceOpen} title="编辑服务设置" onCancel={() => setServiceOpen(false)} onOk={() => serviceForm.submit()} confirmLoading={saveServices.isPending}>
      <Form form={serviceForm} layout="vertical" onFinish={(values) => saveServices.mutate(values)}>
        {services.data?.legacy_kali_runner === false ? (
          <Alert
            type="info"
            showIcon
            message="本部署未启用旧版 Kali Runner"
            description="工具在容器原生 Worker（Muteki）中执行，因此这里不再需要 Runner 地址。如需恢复旧行为，在后端设置 APP_LEGACY_KALI_RUNNER=1。"
            style={{ marginBottom: 16 }}
          />
        ) : (
          <>
            <Alert type="info" showIcon message="Runner 允许配置私网地址。" style={{ marginBottom: 16 }} />
            <Form.Item name="runner_url" label="Kali Runner 地址" rules={[{ required: true, type: "url" }]}><Input /></Form.Item>
          </>
        )}
        <Form.Item
          name="default_model_config_id"
          label="全局默认模型"
          extra="未单独指定模型的连接（如竞技场连接）会回退到这里的配置；留空表示不设全局默认。"
        >
          <Select
            allowClear
            placeholder="不设全局默认"
            options={enabledConfigs.map((item) => ({
              value: item.id,
              label: `${item.name}${item.model_name ? ` · ${item.model_name}` : ""}`,
            }))}
            loading={configs.isLoading}
          />
        </Form.Item>
      </Form>
    </Modal>

    <Modal open={Boolean(bindingConfig)} title={`配置 ${bindingConfig?.name ?? "模型"} 的 Skills`} onCancel={() => setBindingConfig(undefined)} onOk={() => saveBindings.mutate()} confirmLoading={saveBindings.isPending} width={720}>
      <ModelSkillBinding skills={skills.data ?? []} value={bindingValues} onChange={setBindingValues} />
    </Modal>
  </>;
}
