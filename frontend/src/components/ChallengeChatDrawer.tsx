import { DeleteOutlined, PlusOutlined, SendOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Drawer, Empty, Form, Input, List, message, Modal, Select, Space, Spin, Tag } from "antd";
import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { Challenge } from "../types/api";

export function ChallengeChatDrawer({ challenge, onClose }: { challenge?: Challenge; onClose: () => void }) {
  const client = useQueryClient();
  const [conversationId, setConversationId] = useState<string>();
  const [text, setText] = useState("");
  const [form] = Form.useForm();
  const conversations = useQuery({ queryKey: ["conversations", challenge?.id], queryFn: () => api.listConversations(challenge!.id), enabled: Boolean(challenge) });
  const details = useQuery({ queryKey: ["conversation", conversationId], queryFn: () => api.getConversation(conversationId!), enabled: Boolean(conversationId) });
  const messages = useQuery({ queryKey: ["conversation-messages", conversationId], queryFn: () => api.listConversationMessages(conversationId!), enabled: Boolean(conversationId) });
  const models = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  useEffect(() => { if (!conversationId && conversations.data?.[0]) setConversationId(conversations.data[0].id); }, [conversationId, conversations.data]);
  const create = useMutation({ mutationFn: (value: Record<string, unknown>) => api.createConversation(challenge!.id, value), onSuccess: value => { void client.invalidateQueries({ queryKey: ["conversations", challenge?.id] }); setConversationId(value.id); }, onError: (error: Error) => message.error(error.message) });
  const send = useMutation({ mutationFn: () => api.sendConversationMessage(conversationId!, text), onSuccess: () => { setText(""); void client.invalidateQueries({ queryKey: ["conversation-messages", conversationId] }); }, onError: (error: Error) => message.error(error.message) });
  const remove = useMutation({ mutationFn: () => api.deleteConversation(conversationId!), onSuccess: () => { setConversationId(undefined); void client.invalidateQueries({ queryKey: ["conversations", challenge?.id] }); }, onError: (error: Error) => message.error(error.message) });
  const createRun = useMutation({ mutationFn: () => api.createRunFromConversation(conversationId!, { engine_type: "openai_compatible", model_config_id: details.data?.model_config_id, max_agent_steps: 120, max_tool_calls: 120, max_runtime_seconds: 900, max_total_runtime_seconds: 3600, max_context_observations: 8, selected_skill_ids: details.data?.skills.map(item => item.skill_id) ?? [] }), onError: (error: Error) => message.error(error.message) });
  const newConversation = () => { form.resetFields(); Modal.confirm({ title: "新建题目对话", content: <Form form={form} layout="vertical" initialValues={{ title: "新对话", skill_ids: [] }}><Form.Item name="title" label="对话标题" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="model_config_id" label="模型配置" rules={[{ required: true, message: "请选择模型配置" }]}><Select options={(models.data ?? []).filter(item => item.enabled).map(item => ({ value: item.id, label: item.name }))} /></Form.Item><Form.Item name="skill_ids" label="Skills"><Select mode="multiple" options={(skills.data ?? []).filter(item => item.enabled).map(item => ({ value: item.id, label: item.display_name }))} /></Form.Item></Form>, onOk: () => form.validateFields().then(values => create.mutate(values)) }); };
  return <Drawer open={Boolean(challenge)} onClose={onClose} width={760} title={`${challenge?.name ?? "题目"} / 对话`} extra={<Button icon={<PlusOutlined />} onClick={newConversation}>新建对话</Button>}>
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Select value={conversationId} onChange={setConversationId} placeholder="Choose a discussion" options={(conversations.data ?? []).map(item => ({ value: item.id, label: item.title }))} />
      {details.data && <Space wrap><Tag>模型：{models.data?.find(item => item.id === details.data?.model_config_id)?.name ?? "未配置"}</Tag>{details.data.skills.map(item => <Tag key={item.skill_id} color="cyan">{skills.data?.find(skill => skill.id === item.skill_id)?.display_name ?? item.skill_id}</Tag>)}<Button type="link" onClick={() => createRun.mutate()} loading={createRun.isPending}>基于对话创建任务</Button><Button danger type="link" icon={<DeleteOutlined />} onClick={() => Modal.confirm({ title: "删除这条对话？", okText: "删除", cancelText: "取消", onOk: () => remove.mutate() })}>删除</Button></Space>}
      {!conversationId ? <Empty description="请选择或新建对话" /> : <><List loading={messages.isLoading} dataSource={messages.data} locale={{ emptyText: "暂无消息" }} renderItem={item => <List.Item><List.Item.Meta title={item.role === "assistant" ? "模型" : item.role === "user" ? "你" : "系统"} description={item.status === "GENERATING" ? <Spin size="small" /> : <span style={{ whiteSpace: "pre-wrap" }}>{item.content || item.error_message}</span>} /></List.Item>} /><Space.Compact style={{ width: "100%" }}><Input.TextArea value={text} onChange={event => setText(event.target.value)} placeholder="仅讨论题目，不执行工具或访问目标。" autoSize={{ minRows: 2, maxRows: 5 }} /><Button type="primary" icon={<SendOutlined />} loading={send.isPending} disabled={!text.trim()} onClick={() => send.mutate()}>发送</Button></Space.Compact></>}
    </Space>
  </Drawer>;
}
