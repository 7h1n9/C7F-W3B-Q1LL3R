import {
  CommentOutlined,
  EditOutlined,
  EyeOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Upload,
  type UploadFile,
} from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChallengeChatDrawer } from "../components/ChallengeChatDrawer";
import { api } from "../services/api";
import type { Challenge } from "../types/api";

type ChallengePayload = Omit<Challenge, "id" | "created_at" | "updated_at">;
type ChallengeFormValues = Omit<ChallengePayload, "allowed_hosts" | "source_path" | "metadata_json"> & {
  allowed_hosts?: string;
  source_path?: string;
};

function toPayload(values: ChallengeFormValues): ChallengePayload {
  const traffic = values.challenge_type === "TRAFFIC_ANALYSIS";
  const allowedHosts = (values.allowed_hosts ?? "")
    .split(/[,\n;]+/)
    .map((host) => host.trim())
    .filter(Boolean)
    .map((host) => {
      try {
        return new URL(host.includes("://") ? host : `http://${host}`).hostname;
      } catch {
        return host;
      }
    });
  return {
    ...values,
    target_url: traffic ? null : values.target_url,
    allowed_hosts: traffic ? [] : allowedHosts,
    source_path: values.source_path || null,
    flag_pattern: values.flag_pattern || "flag\\{[^}]+\\}",
    status: traffic ? "DRAFT" : values.status || "ACTIVE",
    metadata_json: {},
  };
}

export function ChallengesPage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [editorOpen, setEditorOpen] = useState(false);
  const [detail, setDetail] = useState<Challenge>();
  const [editing, setEditing] = useState<Challenge>();
  const [runChallenge, setRunChallenge] = useState<Challenge>();
  const [chatChallenge, setChatChallenge] = useState<Challenge>();
  const [pcapFile, setPcapFile] = useState<UploadFile>();
  const [form] = Form.useForm<ChallengeFormValues>();
  const [runForm] = Form.useForm();
  const query = useQuery({ queryKey: ["challenges"], queryFn: api.listChallenges });
  const modelConfigs = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });

  const save = useMutation({
    mutationFn: async (values: ChallengeFormValues) => {
      const payload = toPayload(values);
      const saved = editing ? await api.updateChallenge(editing.id, payload) : await api.createChallenge(payload);
      if (payload.challenge_type === "TRAFFIC_ANALYSIS" && pcapFile?.originFileObj) {
        await api.uploadAttachment(saved.id, pcapFile.originFileObj, true);
      }
      return saved;
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["challenges"] });
      message.success(editing ? "题目已更新" : "题目已创建");
      setEditorOpen(false);
      setEditing(undefined);
      setPcapFile(undefined);
      form.resetFields();
    },
    onError: (error: Error) => message.error(error.message),
  });
  const createRun = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.createRun(runChallenge!.id, values),
    onSuccess: (run) => {
      void client.invalidateQueries({ queryKey: ["runs"] });
      message.success("已创建解题任务");
      setRunChallenge(undefined);
      runForm.resetFields();
      navigate(`/runs/${run.id}`);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const remove = useMutation({
    mutationFn: api.deleteChallenge,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["challenges"] });
      message.success("题目已删除");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const openEditor = (challenge?: Challenge) => {
    setEditing(challenge);
    setPcapFile(undefined);
    form.setFieldsValue(
      (challenge
        ? {
            ...challenge,
            allowed_hosts: challenge.allowed_hosts.join(", "),
            source_path: challenge.source_path ?? undefined,
          }
        : { challenge_type: "WEB_TARGET", status: "ACTIVE", flag_pattern: "flag\\{[^}]+\\}" }) as ChallengeFormValues,
    );
    setEditorOpen(true);
  };
  const openRun = (challenge: Challenge) => {
    setRunChallenge(challenge);
    runForm.setFieldsValue({
      engine_type: "mock",
      max_agent_steps: 12,
      max_tool_calls: 12,
      max_runtime_seconds: 300,
      max_context_observations: 8,
    });
  };

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>靶场题目</h1>
          <p>登记已授权 Web 题目或 PCAP 流量分析题。</p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openEditor()}>
          新建题目
        </Button>
      </div>
      <Card className="panel-card">
        <Table
          className="cyber-table"
          rowKey="id"
          dataSource={query.data}
          loading={query.isLoading}
          locale={{ emptyText: <Empty description="尚未登记靶场题目" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          columns={[
                        {
              title: "状态",
              dataIndex: "status",
              render: (status: string) => {
                if (status === "ACTIVE") return <Tag color="success">可用</Tag>;
                if (status === "SOLVED") return <Tag color="green">已解出</Tag>;
                if (status === "DRAFT") return <Tag color="default">草稿</Tag>;
                return <Tag>{status}</Tag>;
              },
            },
            {
              title: "操作",
              render: (_: unknown, record: Challenge) => (
                <Space>
                  <Button type="link" icon={<CommentOutlined />} onClick={() => setChatChallenge(record)}>对话</Button>
                  <Button type="link" icon={<EyeOutlined />} onClick={() => setDetail(record)}>详情</Button>
                  <Button type="link" icon={<EditOutlined />} onClick={() => openEditor(record)}>编辑</Button>
                  <Button type="link" icon={<PlayCircleOutlined />} onClick={() => openRun(record)} disabled={record.status !== "ACTIVE"}>创建任务</Button>
                  <Popconfirm title="确认删除该题目？" description="相关任务记录不会被自动删除。" onConfirm={() => remove.mutate(record.id)} okText="删除" cancelText="取消">
                    <Button type="link" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal open={editorOpen} title={editing ? "编辑授权题目" : "新建授权题目"} onCancel={() => setEditorOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending} okText="保存" cancelText="取消">
        <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)}>
          <Form.Item name="challenge_type" label="题目模式" rules={[{ required: true }]}>
            <Select
              onChange={(value) => {
                if (value === "TRAFFIC_ANALYSIS") form.setFieldsValue({ target_url: undefined, allowed_hosts: undefined });
              }}
              options={[{ value: "WEB_TARGET", label: "Web 靶场题" }, { value: "TRAFFIC_ANALYSIS", label: "流量分析题" }]}
            />
          </Form.Item>
          <Form.Item name="name" label="题目名称" rules={[{ required: true, message: "请输入题目名称" }]}><Input /></Form.Item>
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.challenge_type !== current.challenge_type}>
            {({ getFieldValue }) => getFieldValue("challenge_type") === "TRAFFIC_ANALYSIS" ? (
              <Form.Item label="PCAP 主附件" required>
                <Upload beforeUpload={() => false} maxCount={1} accept=".pcap,.pcapng,.cap" fileList={pcapFile ? [pcapFile] : []} onChange={({ fileList }) => setPcapFile(fileList[0])}>
                  <Button icon={<UploadOutlined />}>选择 PCAP/PCAPNG</Button>
                </Upload>
                <div className="field-help">上传成功前题目保持 DRAFT，文件会校验扩展名和 PCAP 魔数。</div>
              </Form.Item>
            ) : (
              <>
                <Form.Item name="target_url" label="目标地址" rules={[{ required: true, type: "url", message: "请输入有效的 HTTP(S) 地址" }]}><Input placeholder="http://challenge.local" /></Form.Item>
                <Form.Item name="allowed_hosts" label="允许访问的主机" extra="填写 localhost 或完整 URL，系统会自动提取主机名；多个主机用英文逗号或分号分隔" rules={[{ required: true, message: "请填写允许访问的主机" }]}><Input placeholder="localhost, challenge.local" /></Form.Item>
              </>
            )}
          </Form.Item>
          <Form.Item name="description" label="题目说明"><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="flag_pattern" label="Flag 正则"><Input /></Form.Item>
          <Form.Item name="source_path" label="源码路径（Web 可选）"><Input /></Form.Item>
        </Form>
      </Modal>

      <ChallengeChatDrawer challenge={chatChallenge} onClose={() => setChatChallenge(undefined)} />
      <Modal open={Boolean(detail)} title="题目详情" footer={<Button onClick={() => setDetail(undefined)}>关闭</Button>} onCancel={() => setDetail(undefined)}>
        {detail && <Descriptions column={1} bordered size="small" items={[
          { key: "description", label: "题目说明", children: detail.description || "—" },
          { key: "type", label: "题目模式", children: detail.challenge_type === "TRAFFIC_ANALYSIS" ? "流量分析" : "Web 靶场" },
          { key: "target", label: "目标地址", children: detail.target_url || "不适用" },
          { key: "hosts", label: "允许主机", children: detail.allowed_hosts.length ? detail.allowed_hosts.join(", ") : "不适用" },
          { key: "attachment", label: "主附件", children: detail.primary_attachment_id || "未上传" },
          { key: "flag", label: "Flag 正则", children: detail.flag_pattern },
        ]} />}
      </Modal>

      <Modal open={Boolean(runChallenge)} title="创建解题任务" onCancel={() => setRunChallenge(undefined)} onOk={() => runForm.submit()} confirmLoading={createRun.isPending} okText="创建" cancelText="取消">
        <Form form={runForm} layout="vertical" onFinish={(values) => createRun.mutate(values)}>
          <Form.Item name="engine_type" label="解题引擎" rules={[{ required: true }]}><Select options={[{ value: "mock", label: "Mock" }, { value: "openai_compatible", label: "OpenAI Compatible" }, { value: "codex_sdk", label: "Codex SDK" }]} /></Form.Item>
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.engine_type !== current.engine_type}>{() => runForm.getFieldValue("engine_type") === "openai_compatible" ? <Form.Item name="model_config_id" label="模型配置" rules={[{ required: true, message: "请选择已启用的模型配置" }]}><Select options={(modelConfigs.data ?? []).filter((item) => item.enabled).map((item) => ({ value: item.id, label: item.name }))} /></Form.Item> : null}</Form.Item>
          <Form.Item name="max_agent_steps" label="最大 Agent 步数"><InputNumber min={1} max={100} style={{ width: "100%" }} /></Form.Item>
          <Form.Item name="max_tool_calls" label="最大工具调用次数"><InputNumber min={0} max={100} style={{ width: "100%" }} /></Form.Item>
          <Form.Item name="max_runtime_seconds" label="最大运行时长（秒）"><InputNumber min={10} max={3600} style={{ width: "100%" }} /></Form.Item>
          <Form.Item name="max_context_observations" hidden><InputNumber /></Form.Item>
        </Form>
      </Modal>
    </>
  );
}

