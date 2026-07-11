import { ApiOutlined, KeyOutlined, PlusOutlined, SafetyCertificateOutlined, ToolOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, Empty, Form, Input, message, Modal, Popconfirm, Row, Space, Spin, Switch, Table, Tag } from "antd";
import { useState } from "react";
import { api } from "../services/api";
import { ModelSkillBinding } from "../components/skills/ModelSkillBinding";

type Config = { id: string; name: string; provider_type: string; base_url?: string; model_name?: string; enabled: boolean; api_key_configured: boolean };
type ServiceForm = { runner_url: string; codex_bridge_url: string };

function StatusTag({ reachable }: { reachable?: boolean }) { return <Tag color={reachable ? "success" : "error"}>{reachable ? "服务正常" : "不可达"}</Tag>; }

export function SettingsPage() {
  const client = useQueryClient();
  const [open, setOpen] = useState(false); const [editing, setEditing] = useState<Config>(); const [form] = Form.useForm();
  const [serviceOpen, setServiceOpen] = useState(false); const [serviceForm] = Form.useForm<ServiceForm>();
  const [bindingConfig, setBindingConfig] = useState<Config>();
  const [bindingValues, setBindingValues] = useState<Array<{ skill_id: string; enabled: boolean; priority: number; config_json: Record<string, unknown> }>>([]);
  const configs = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  const services = useQuery({ queryKey: ["system-settings"], queryFn: api.getSystemSettings, refetchInterval: 15_000 });
  const refresh = () => void client.invalidateQueries({ queryKey: ["model-configs"] });
  const refreshServices = () => void client.invalidateQueries({ queryKey: ["system-settings"] });
  const save = useMutation({ mutationFn: (values: Record<string, unknown>) => editing ? api.updateModelConfig(editing.id, values) : api.createModelConfig(values), onSuccess: () => { message.success("模型配置已保存"); setOpen(false); setEditing(undefined); form.resetFields(); refresh(); }, onError: (error: Error) => message.error(error.message) });
  const remove = useMutation({ mutationFn: api.deleteModelConfig, onSuccess: () => { message.success("模型配置已删除"); refresh(); }, onError: (error: Error) => message.error(error.message) });
  const test = useMutation({ mutationFn: api.testModelConfig, onSuccess: (result) => result.ok ? message.success(result.message) : message.error(result.message), onError: (error: Error) => message.error(error.message) });
  const saveServices = useMutation({ mutationFn: api.updateSystemSettings, onSuccess: () => { message.success("服务地址已保存"); setServiceOpen(false); refreshServices(); }, onError: (error: Error) => message.error(error.message) });
  const saveBindings = useMutation({ mutationFn: async () => api.setModelSkills(bindingConfig!.id, bindingValues), onSuccess: () => { message.success("Skills 绑定已保存"); setBindingConfig(undefined); }, onError: (error: Error) => message.error(error.message) });
  const edit = (item?: Config) => { setEditing(item); form.setFieldsValue(item ? { ...item, api_key: undefined } : { provider_type: "openai_compatible", enabled: true }); setOpen(true); };
  const editServices = () => { if (services.data) { serviceForm.setFieldsValue({ runner_url: services.data.runner_url, codex_bridge_url: services.data.codex_bridge_url }); } setServiceOpen(true); };
  const editBindings = async (item: Config) => { setBindingConfig(item); const rows = await api.getModelSkills(item.id); setBindingValues(rows as typeof bindingValues); };
  return <>
    <div className="page-heading"><div><h1>系统配置</h1><p>管理模型接入及本地执行服务状态。</p></div><Button type="primary" icon={<PlusOutlined />} onClick={() => edit()}>新建模型配置</Button></div>
    <Alert className="panel-card" type="warning" showIcon icon={<KeyOutlined />} message="API 密钥与 Runner Token 均为仅写入字段；前端只显示是否已配置。" />
    <Card className="panel-card" title="模型配置" style={{ marginTop: 18 }}><Table className="cyber-table" rowKey="id" dataSource={configs.data} loading={configs.isLoading} locale={{ emptyText: <Empty description="尚未配置模型服务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
      { title: "名称", dataIndex: "name" }, { title: "服务地址", dataIndex: "base_url", ellipsis: true }, { title: "模型", dataIndex: "model_name" }, { title: "密钥", dataIndex: "api_key_configured", render: (value: boolean) => value ? <Tag color="success">已配置</Tag> : <Tag>未配置</Tag> }, { title: "状态", dataIndex: "enabled", render: (value: boolean) => <Tag color={value ? "success" : "default"}>{value ? "已启用" : "已禁用"}</Tag> }, { title: "操作", render: (_, item: Config) => <Space><Button type="link" onClick={() => edit(item)}>编辑</Button><Button type="link" onClick={() => void editBindings(item)}>配置 Skills</Button><Button type="link" onClick={() => test.mutate(item.id)}>测试连接</Button><Popconfirm title="删除该模型配置？" onConfirm={() => remove.mutate(item.id)}><Button type="link" danger>删除</Button></Popconfirm></Space> },
    ]} /></Card>
    <Card className="panel-card" title="执行服务" extra={<Space><Button onClick={refreshServices}>刷新状态</Button><Button type="primary" onClick={editServices}>编辑服务地址</Button></Space>} style={{ marginTop: 18 }}>
      {services.isLoading ? <Spin /> : <Descriptions column={{ xs: 1, md: 2 }} items={[
        { key: "runner-url", label: "Kali Runner 地址", children: services.data?.runner_url ?? "—" }, { key: "runner-state", label: "Kali Runner 状态", children: <StatusTag reachable={services.data?.runner.reachable} /> }, { key: "runner-cidr", label: "Runner 允许网段", children: services.data?.runner_allowed_cidrs ?? "—" }, { key: "runner-token", label: "Runner Token", children: services.data?.runner_token_configured ? <Tag color="success">已配置</Tag> : <Tag color="error">未配置</Tag> }, { key: "bridge-url", label: "Codex Bridge 地址", children: services.data?.codex_bridge_url ?? "—" }, { key: "bridge-state", label: "Codex Bridge 状态", children: <StatusTag reachable={services.data?.codex_bridge.reachable} /> }, { key: "bridge-mode", label: "Codex 模式", children: typeof services.data?.codex_bridge.details === "object" && services.data.codex_bridge.details.mock_mode === true ? <Tag color="blue">Mock</Tag> : "运行时" },
      ]} />}
    </Card>
    <Row gutter={[18, 18]} style={{ marginTop: 18 }}><Col xs={24} md={8}><Card className="panel-card" title={<><ApiOutlined /> OpenAI 兼容模型</>}>执行前仅发送最小安全测试请求。</Card></Col><Col xs={24} md={8}><Card className="panel-card" title={<><ToolOutlined /> Codex SDK 桥接服务</>}>地址可从本地界面查看或调整；运行模式来自健康检查。</Card></Col><Col xs={24} md={8}><Card className="panel-card" title={<><SafetyCertificateOutlined /> Kali 执行端</>}>令牌、主机白名单和工作区边界均在执行端再次校验。</Card></Col></Row>
    <Modal open={open} title={editing ? "编辑模型配置" : "新建模型配置"} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending}><Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)}><Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="base_url" label="Base URL" rules={[{ required: true, type: "url" }]}><Input placeholder="https://api.example.com/v1" /></Form.Item><Form.Item name="model_name" label="模型名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="api_key" label={editing ? "新 API Key（留空则不变）" : "API Key"} rules={editing ? [] : [{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item><Form.Item name="provider_type" hidden><Input /></Form.Item><Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></Form></Modal>
    <Modal open={serviceOpen} title="编辑服务地址" onCancel={() => setServiceOpen(false)} onOk={() => serviceForm.submit()} confirmLoading={saveServices.isPending}><Alert type="info" showIcon message="Runner 仅允许配置环境变量声明的私网网段；Codex Bridge 仅允许本机地址。" style={{ marginBottom: 16 }} /><Form form={serviceForm} layout="vertical" onFinish={(values) => saveServices.mutate(values)}><Form.Item name="runner_url" label="Kali Runner 地址" rules={[{ required: true, type: "url" }]}><Input /></Form.Item><Form.Item name="codex_bridge_url" label="Codex Bridge 地址" rules={[{ required: true, type: "url" }]}><Input /></Form.Item></Form></Modal>
    <Modal open={Boolean(bindingConfig)} title={`配置 ${bindingConfig?.name ?? "模型"} 的 Skills`} onCancel={() => setBindingConfig(undefined)} onOk={() => saveBindings.mutate()} confirmLoading={saveBindings.isPending} width={720}><ModelSkillBinding skills={skills.data ?? []} value={bindingValues} onChange={setBindingValues} /></Modal>
  </>;
}
