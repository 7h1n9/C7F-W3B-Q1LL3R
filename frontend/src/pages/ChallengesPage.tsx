import { EditOutlined, EyeOutlined, PlayCircleOutlined, PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Descriptions, Empty, Form, Input, message, Modal, Popconfirm, Space, Table, Tag } from "antd";
import { useState } from "react";
import { api } from "../services/api";
import type { Challenge } from "../types/api";

type ChallengePayload = Omit<Challenge, "id" | "created_at" | "updated_at">;
type ChallengeFormValues = Omit<ChallengePayload, "allowed_hosts" | "source_path"> & { allowed_hosts: string; source_path?: string };

function toPayload(values: ChallengeFormValues): ChallengePayload {
  return { ...values, description: values.description ?? "", allowed_hosts: values.allowed_hosts.split(",").map((host) => host.trim()).filter(Boolean), source_path: values.source_path || null, flag_pattern: values.flag_pattern || "flag\\{[^}]+\\}", status: values.status || "ACTIVE" };
}

export function ChallengesPage() {
  const client = useQueryClient();
  const [editorOpen, setEditorOpen] = useState(false);
  const [detail, setDetail] = useState<Challenge>();
  const [editing, setEditing] = useState<Challenge>();
  const [form] = Form.useForm<ChallengeFormValues>();
  const query = useQuery({ queryKey: ["challenges"], queryFn: api.listChallenges });
  const save = useMutation({
    mutationFn: (payload: ChallengePayload) => editing ? api.updateChallenge(editing.id, payload) : api.createChallenge(payload),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["challenges"] }); message.success(editing ? "题目已更新" : "题目已创建"); setEditorOpen(false); setEditing(undefined); form.resetFields(); },
    onError: (error: Error) => message.error(error.message),
  });
  const createRun = useMutation({ mutationFn: (id: string) => api.createRun(id), onSuccess: () => { void client.invalidateQueries({ queryKey: ["runs"] }); message.success("已创建解题任务"); }, onError: (error: Error) => message.error(error.message) });
  const remove = useMutation({ mutationFn: api.deleteChallenge, onSuccess: () => { void client.invalidateQueries({ queryKey: ["challenges"] }); message.success("题目已删除"); }, onError: (error: Error) => message.error(error.message) });
  const openEditor = (challenge?: Challenge) => { setEditing(challenge); form.setFieldsValue(challenge ? { ...challenge, allowed_hosts: challenge.allowed_hosts.join(", "), source_path: challenge.source_path ?? undefined } : { status: "ACTIVE", flag_pattern: "flag\\{[^}]+\\}" }); setEditorOpen(true); };

  return <>
    <div className="page-heading"><div><h1>靶场题目</h1><p>仅登记已授权 CTF 题目与本地靶场目标。</p></div><Button type="primary" icon={<PlusOutlined />} onClick={() => openEditor()}>新建题目</Button></div>
    <Card className="panel-card">
      <Table className="cyber-table" rowKey="id" dataSource={query.data} loading={query.isLoading} locale={{ emptyText: <Empty description="尚未登记靶场题目" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
        { title: "题目名称", dataIndex: "name", render: (name: string, record: Challenge) => <Space direction="vertical" size={0}><strong>{name}</strong><span className="id-code">{record.id.slice(0, 8)}</span></Space> },
        { title: "目标地址", dataIndex: "target_url", ellipsis: true },
        { title: "允许主机", dataIndex: "allowed_hosts", render: (hosts: string[]) => hosts.map((host) => <Tag color="cyan" key={host}>{host}</Tag>) },
        { title: "状态", dataIndex: "status", render: (status: string) => <Tag color={status === "ACTIVE" ? "success" : "default"}>{status === "ACTIVE" ? "可用" : status}</Tag> },
        { title: "操作", render: (_, record: Challenge) => <Space><Button type="link" icon={<EyeOutlined />} onClick={() => setDetail(record)}>详情</Button><Button type="link" icon={<EditOutlined />} onClick={() => openEditor(record)}>编辑</Button><Button type="link" icon={<PlayCircleOutlined />} onClick={() => createRun.mutate(record.id)}>创建任务</Button><Popconfirm title="确认删除该题目？" description="相关任务记录不会被自动删除。" onConfirm={() => remove.mutate(record.id)} okText="删除" cancelText="取消"><Button type="link" danger>删除</Button></Popconfirm></Space> },
      ]} />
    </Card>
    <Modal open={editorOpen} title={editing ? "编辑授权题目" : "新建授权题目"} onCancel={() => setEditorOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending} okText="保存" cancelText="取消">
      <Form form={form} layout="vertical" onFinish={(values) => save.mutate(toPayload(values))}>
        <Form.Item name="name" label="题目名称" rules={[{ required: true, message: "请输入题目名称" }]}><Input /></Form.Item>
        <Form.Item name="target_url" label="目标地址" rules={[{ required: true, type: "url", message: "请输入有效的 HTTP(S) 地址" }]}><Input placeholder="http://challenge.local" /></Form.Item>
        <Form.Item name="allowed_hosts" label="允许访问的主机" extra="多个主机使用英文逗号分隔" rules={[{ required: true, message: "请填写允许访问的主机" }]}><Input placeholder="challenge.local" /></Form.Item>
        <Form.Item name="description" label="题目说明"><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name="flag_pattern" label="Flag 正则"><Input /></Form.Item>
        <Form.Item name="source_path" label="源码或附件路径"><Input /></Form.Item>
      </Form>
    </Modal>
    <Modal open={Boolean(detail)} title="题目详情" footer={<Button onClick={() => setDetail(undefined)}>关闭</Button>} onCancel={() => setDetail(undefined)}>
      {detail && <Descriptions column={1} bordered size="small" items={[
        { key: "description", label: "题目说明", children: detail.description || "—" }, { key: "target", label: "目标地址", children: detail.target_url }, { key: "hosts", label: "允许主机", children: detail.allowed_hosts.join(", ") }, { key: "flag", label: "Flag 正则", children: detail.flag_pattern }, { key: "source", label: "源码路径", children: detail.source_path || "—" },
      ]} />}
    </Modal>
  </>;
}
