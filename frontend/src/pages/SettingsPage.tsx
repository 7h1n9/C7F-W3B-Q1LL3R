import { ApiOutlined, KeyOutlined, PlusOutlined, SafetyCertificateOutlined, ToolOutlined } from "@ant-design/icons";
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
  Popconfirm,
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
import { ModelSkillBinding } from "../components/skills/ModelSkillBinding";
import { api } from "../services/api";
import type { ModelConfig } from "../types/api";

type ServiceForm = { runner_url: string };
type SkillBinding = { skill_id: string; enabled: boolean; priority: number; config_json: Record<string, unknown> };
type ModelConfigFormValues = {
  name?: string;
  provider_type?: ModelConfig["provider_type"];
  base_url?: string;
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
};

const modelConfigFields: Array<keyof ModelConfigFormValues> = [
  "name", "provider_type", "base_url", "wire_api", "model_name", "reasoning_effort", "api_key", "enabled", "roles",
  "action_protocol", "structured_output_mode", "request_timeout_seconds", "max_output_tokens",
  "temperature", "max_retries", "retry_base_seconds", "rate_limit_cooldown_seconds",
  "requests_per_minute", "max_concurrency", "context_token_limit",
];

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
  return payload;
}

function StatusTag({ reachable }: { reachable?: boolean }) {
  return <Tag color={reachable ? "success" : "error"}>{reachable ? "服务正常" : "不可达"}</Tag>;
}

function roleLabel(role: string) {
  return role === "coordinator_reason" ? "Coordinator Reason" : "Worker";
}

function providerLabel(provider: string) {
  return provider === "codex_cli" ? "Codex CLI / API" : "OpenAI-compatible";
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
  const configs = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  const services = useQuery({ queryKey: ["system-settings"], queryFn: api.getSystemSettings, refetchInterval: 15_000 });
  const refresh = () => void client.invalidateQueries({ queryKey: ["model-configs"] });
  const refreshServices = () => void client.invalidateQueries({ queryKey: ["system-settings"] });

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
    mutationFn: api.deleteModelConfig,
    onSuccess: () => { message.success("模型配置已删除"); refresh(); },
    onError: (error: Error) => message.error(error.message),
  });
  const test = useMutation({
    mutationFn: api.testModelConfig,
    onSuccess: (result) => result.ok ? message.success(result.message) : message.error(result.message),
    onError: (error: Error) => message.error(error.message),
  });
  const saveServices = useMutation({
    mutationFn: api.updateSystemSettings,
    onSuccess: () => { message.success("执行服务地址已保存"); setServiceOpen(false); refreshServices(); },
    onError: (error: Error) => message.error(error.message),
  });
  const saveBindings = useMutation({
    mutationFn: async () => api.setModelSkills(bindingConfig!.id, bindingValues),
    onSuccess: () => { message.success("Skills 绑定已保存"); setBindingConfig(undefined); },
    onError: (error: Error) => message.error(error.message),
  });

  const edit = (item?: ModelConfig) => {
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
    } : { provider_type: "openai_compatible", enabled: true, roles: ["worker"] });
    setOpen(true);
  };
  const editServices = () => {
    serviceForm.resetFields();
    if (services.data) serviceForm.setFieldsValue({ runner_url: services.data.runner_url });
    setServiceOpen(true);
  };
  const editBindings = async (item: ModelConfig) => {
    setBindingConfig(item);
    setBindingValues(await api.getModelSkills(item.id) as SkillBinding[]);
  };

  const configColumns = [
    { title: "名称", dataIndex: "name", width: 180 },
    { title: "提供方", dataIndex: "provider_type", width: 160, render: (value: string) => <Tag color={value === "codex_cli" ? "purple" : "blue"}>{providerLabel(value)}</Tag> },
    { title: "模型", dataIndex: "model_name", width: 180, render: (value: string, item: ModelConfig) => <Space direction="vertical" size={0}><span>{value || "—"}</span>{item.provider_type === "codex_cli" ? <Tag color="purple">effort: {item.reasoning_effort ?? "medium"}</Tag> : null}</Space> },
    { title: "Base URL", dataIndex: "base_url", width: 260, ellipsis: true },
    {
      title: "Muteki 角色",
      dataIndex: "roles",
      width: 230,
      render: (roles?: ModelConfig["roles"]) => <Space wrap>{(roles ?? ["worker"]).map((role) => <Tag key={role} color={role === "coordinator_reason" ? "purple" : "blue"}>{roleLabel(role)}</Tag>)}</Space>,
    },
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
          <Button type="link" onClick={() => test.mutate(item.id)}>测试连接</Button>
          <Popconfirm title="确认删除模型配置？" onConfirm={() => remove.mutate(item.id)}>
            <Button type="link" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return <>
    <div className="page-heading">
      <div><h1>系统配置</h1><p>配置 Coordinator Reason、Worker 引擎和执行服务。</p></div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => edit()}>新建模型配置</Button>
    </div>
    <Alert className="panel-card" type="warning" showIcon icon={<KeyOutlined />} message="API Key 只写入后端，前端仅显示是否已配置。" />

    <Card className="panel-card" title="模型配置" style={{ marginTop: 18 }}>
      <Table className="cyber-table" rowKey="id" dataSource={configs.data} loading={configs.isLoading} scroll={{ x: 1420 }} tableLayout="fixed" locale={{ emptyText: <Empty description="尚未配置模型服务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={configColumns} />
    </Card>

    <Card className="panel-card" title="Muteki Runtime 分工" style={{ marginTop: 18 }}>
      <Alert type="info" showIcon message="Coordinator Reason 只读取 Blackboard 并生成 Intent；Worker 引擎负责执行 Intent。创建任务时可以选择多个 Worker 并行调度。" />
      <Space wrap style={{ marginTop: 14 }}>{(configs.data ?? []).map((item) => <Tag key={item.id} color={item.enabled ? "green" : "default"}>{item.name} · {(item.roles ?? ["worker"]).map(roleLabel).join(" / ")}</Tag>)}</Space>
    </Card>

    <Card className="panel-card" title="执行服务" extra={<Space><Button onClick={refreshServices}>刷新状态</Button><Button type="primary" onClick={editServices}>编辑服务地址</Button></Space>} style={{ marginTop: 18 }}>
      {services.isLoading ? <Spin /> : <Descriptions column={{ xs: 1, md: 2 }} items={[
        { key: "runner-url", label: "Kali Runner 地址", children: services.data?.runner_url ?? "—" },
        { key: "runner-state", label: "Runner 状态", children: <StatusTag reachable={services.data?.runner.reachable} /> },
        { key: "runner-cidr", label: "Runner 允许网段", children: services.data?.runner_allowed_cidrs ?? "—" },
        { key: "runner-token", label: "Runner Token", children: services.data?.runner_token_configured ? <Tag color="success">已配置</Tag> : <Tag color="error">未配置</Tag> },
      ]} />}
    </Card>

    <Tabs className="panel-card" style={{ marginTop: 18 }} items={[
      { key: "reason", label: "Coordinator Reason", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "source", label: "职责", children: "读取 Blackboard，输出受策略约束的 Intent。" }, { key: "fallback", label: "失败回退", children: "使用 Muteki 本地策略规划器，不会让 Reason 直接执行工具。" }, { key: "output", label: "输出", children: "verdict + typed intents，不包含 Worker 原始响应。" }]} /> },
      { key: "worker", label: "Worker Engines", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "parallel", label: "并行", children: "按创建任务时勾选的 EngineProfile 调度。" }, { key: "supported", label: "可选引擎", children: "Codex CLI / OpenAI-compatible。" }, { key: "boundary", label: "执行边界", children: "Codex 走官方 Worker/Sandbox；兼容模型通过受限 Worker 适配器进入 Tool Gateway。" }]} /> },
      { key: "tools", label: "Tool Policy", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "allow", label: "工具边界", children: "题型、Worker 角色和 Runner Policy 共同约束。" }, { key: "risk", label: "风险等级", children: "由现有安全层和 Tool Gateway 继续控制。" }]} /> },
      { key: "audit", label: "数据与审计", children: <Descriptions column={{ xs: 1, md: 2 }} items={[{ key: "redact", label: "敏感字段脱敏", children: "启用。" }, { key: "summary", label: "模型响应", children: "只保留安全摘要和 Token 用量，不保存原始响应。" }]} /> },
    ]} />

    <Row gutter={[18, 18]} style={{ marginTop: 18 }}>
      <Col xs={24} md={8}><Card className="panel-card" title={<><ApiOutlined /> OpenAI-compatible</>}>可配置 Step、DeepSeek 等兼容 Chat Completions 的模型，并分别指定 Coordinator Reason 或 Worker 角色。</Card></Col>
      <Col xs={24} md={8}><Card className="panel-card" title={<><ToolOutlined /> Codex CLI / API</>}>通过 Muteki 官方 Worker/Sandbox 适配器执行；支持本地订阅或配置 Responses API 端点。</Card></Col>
      <Col xs={24} md={8}><Card className="panel-card" title={<><SafetyCertificateOutlined /> Security Boundary</>}>模型只能生成受约束的 Intent；实际工具执行仍经过现有安全层和 Tool Gateway。</Card></Col>
    </Row>

    <Modal open={open} title={editing ? "编辑模型配置" : "新建模型配置"} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending}>
      <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values as ModelConfigFormValues)}>
        <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="provider_type" label="提供方" rules={[{ required: true }]}><Select options={[{ value: "openai_compatible", label: "OpenAI-compatible（API）" }, { value: "codex_cli", label: "Codex CLI / API（Responses）" }]} onChange={(value) => { if (value === "codex_cli") form.setFieldsValue({ roles: ["worker"], reasoning_effort: "medium" }); }} /></Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type}>{({ getFieldValue }) => getFieldValue("provider_type") === "codex_cli" ? <Form.Item name="base_url" label="Base URL（可选，API 模式必填）" rules={[{ type: "url" }]} extra="留空时使用本机/容器 Codex 登录态；填写后按 model_providers.muteki 走 Responses API"><Input placeholder="https://api.openai.com/v1" /></Form.Item> : <Form.Item name="base_url" label="Base URL" rules={[{ required: true, type: "url" }]}><Input placeholder="https://api.example.com/v1" /></Form.Item>}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type || previous.base_url !== current.base_url}>{({ getFieldValue }) => {
          const provider = getFieldValue("provider_type");
          const baseUrl = getFieldValue("base_url");
          if (provider === "codex_cli" && baseUrl) {
            return <Form.Item name="wire_api" label="Wire API 协议" rules={[{ required: true }]} extra="codex 的 Responses API 填 responses；若端点兼容 Chat Completions 可填 chat_completions"><Select options={[{ value: "responses", label: "responses（默认）" }, { value: "chat_completions", label: "chat_completions" }]} /></Form.Item>;
          }
          return null;
        }}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type}>{({ getFieldValue }) => getFieldValue("provider_type") === "codex_cli" ? <Collapse style={{ marginBottom: 16 }} items={[{ key: "codex-profile", label: "Codex CLI Worker 参数（点击展开选择）", children: <><Form.Item name="model_name" label="模型名称" rules={[{ required: true }]}><Select mode="tags" maxCount={1} placeholder="????????" showSearch filterOption={((input, option) => (option?.label ?? "").toLowerCase().includes(input.toLowerCase()))} options={["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"].map((value) => ({ value, label: value }))} /></Form.Item><Form.Item name="reasoning_effort" label="推理强度" rules={[{ required: true }]} extra="只选择强度，不要拼接到模型名；实际传给 Codex CLI 的是 model_reasoning_effort。"><Select options={[{ value: "none", label: "none（关闭）" }, { value: "low", label: "low" }, { value: "medium", label: "medium（默认）" }, { value: "high", label: "high（推荐）" }, { value: "xhigh", label: "xhigh" }, { value: "max", label: "max" }, { value: "ultra", label: "ultra（若本机 CLI 支持）" }]} /></Form.Item></> }]} /> : <Form.Item name="model_name" label="模型名称" rules={[{ required: true }]}><Input placeholder="step-xxx / deepseek-chat" /></Form.Item>}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type || previous.base_url !== current.base_url}>{({ getFieldValue }) => {
          const provider = getFieldValue("provider_type");
          const baseUrl = getFieldValue("base_url");
          if (provider === "openai_compatible" || (provider === "codex_cli" && baseUrl)) {
            return <Form.Item name="api_key" label={editing ? "新 API Key（留空则不变）" : "API Key"} rules={editing ? [] : [{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item>;
          }
          return null;
        }}</Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.provider_type !== current.provider_type}>{({ getFieldValue }) => <Form.Item name="roles" label="Muteki 角色" rules={[{ required: true }]}><Select mode="multiple" disabled={getFieldValue("provider_type") === "codex_cli"} options={getFieldValue("provider_type") === "codex_cli" ? [{ value: "worker", label: "Worker（执行引擎）" }] : [{ value: "coordinator_reason", label: "Coordinator Reason（协调器推理）" }, { value: "worker", label: "Worker（执行引擎）" }]} /></Form.Item>}</Form.Item>
        <Form.Item name="action_protocol" label="Action Protocol"><Input placeholder="json_schema / json_object / prompt_json" /></Form.Item>
        <Form.Item name="structured_output_mode" label="Structured Output Mode"><Input placeholder="json_schema / json_object / prompt_json" /></Form.Item>
        <Row gutter={12}><Col span={12}><Form.Item name="request_timeout_seconds" label="Timeout"><Input type="number" /></Form.Item></Col><Col span={12}><Form.Item name="max_output_tokens" label="Max Output Tokens"><Input type="number" /></Form.Item></Col></Row>
        <Row gutter={12}><Col span={12}><Form.Item name="temperature" label="Temperature"><Input type="number" step="0.1" /></Form.Item></Col><Col span={12}><Form.Item name="context_token_limit" label="Context Token Limit"><Input type="number" /></Form.Item></Col></Row>
        <Row gutter={12}><Col span={12}><Form.Item name="max_retries" label="Max Retries"><Input type="number" /></Form.Item></Col><Col span={12}><Form.Item name="retry_base_seconds" label="Retry Backoff"><Input type="number" /></Form.Item></Col></Row>
        <Row gutter={12}><Col span={12}><Form.Item name="rate_limit_cooldown_seconds" label="429 Cooldown"><Input type="number" /></Form.Item></Col><Col span={12}><Form.Item name="requests_per_minute" label="RPM"><Input type="number" /></Form.Item></Col></Row>
        <Form.Item name="max_concurrency" label="Max Concurrency"><Input type="number" /></Form.Item>
        <Form.Item name="provider_type" hidden><Input /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>

    <Modal open={serviceOpen} title="编辑服务地址" onCancel={() => setServiceOpen(false)} onOk={() => serviceForm.submit()} confirmLoading={saveServices.isPending}>
      <Alert type="info" showIcon message="Runner 允许配置私网地址。" style={{ marginBottom: 16 }} />
      <Form form={serviceForm} layout="vertical" onFinish={(values) => saveServices.mutate(values)}>
        <Form.Item name="runner_url" label="Kali Runner 地址" rules={[{ required: true, type: "url" }]}><Input /></Form.Item>
      </Form>
    </Modal>

    <Modal open={Boolean(bindingConfig)} title={`配置 ${bindingConfig?.name ?? "模型"} 的 Skills`} onCancel={() => setBindingConfig(undefined)} onOk={() => saveBindings.mutate()} confirmLoading={saveBindings.isPending} width={720}>
      <ModelSkillBinding skills={skills.data ?? []} value={bindingValues} onChange={setBindingValues} />
    </Modal>
  </>;
}