import { Form, Input, Modal, Select, Switch } from "antd";
import type { Skill } from "../../types/api";
import { SkillPreview } from "./SkillPreview";

export type SkillForm = Omit<Skill, "id" | "source_type" | "version" | "binding_count" | "created_at" | "updated_at">;

export function SkillEditor({ open, value, loading, onClose, onSave }: { open: boolean; value?: Skill; loading?: boolean; onClose: () => void; onSave: (value: Record<string, unknown>) => void }) {
  const [form] = Form.useForm();
  const builtin = value?.source_type === "BUILTIN";
  return <Modal open={open} title={value ? `${builtin ? "内置 Skill（只读）" : "编辑 Skill"}` : "新建 Skill"} width={980} okText={builtin ? "关闭" : "验证并保存"} cancelText="取消" okButtonProps={{ disabled: builtin }} confirmLoading={loading} onCancel={onClose} onOk={() => builtin ? onClose() : form.submit()}>
    <Form form={form} layout="vertical" initialValues={value ?? { challenge_types: ["WEB_TARGET"], allowed_tools: [], risk_level: "low", enabled: true }} onFinish={onSave} key={value?.id ?? "new"}>
      <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input disabled={builtin} /></Form.Item>
      <Form.Item name="display_name" label="显示名称" rules={[{ required: true }]}><Input disabled={builtin} /></Form.Item>
      <Form.Item name="description" label="说明"><Input.TextArea disabled={builtin} rows={2} /></Form.Item>
      <Form.Item name="challenge_types" label="适用题型"><Select disabled={builtin} mode="multiple" options={[{ value: "WEB_TARGET", label: "Web 靶场题" }, { value: "TRAFFIC_ANALYSIS", label: "流量分析题" }]} /></Form.Item>
      <Form.Item name="allowed_tools" label="允许工具"><Select disabled={builtin} mode="multiple" options={["http_request", "file_read", "file_search", "python_run", "pcap_metadata", "pcap_protocols", "pcap_query"].map(value => ({ value }))} /></Form.Item>
      <Form.Item name="risk_level" label="风险等级"><Select disabled={builtin} options={["low", "medium", "high"].map(value => ({ value, label: value }))} /></Form.Item>
      <Form.Item name="enabled" label="启用状态" valuePropName="checked"><Switch disabled={builtin} /></Form.Item>
      <Form.Item noStyle shouldUpdate>{() => <><Form.Item name="content_markdown" label="Markdown 内容" rules={[{ required: true }]}><Input.TextArea disabled={builtin} rows={12} /></Form.Item><SkillPreview content={form.getFieldValue("content_markdown") ?? ""} /></>}</Form.Item>
    </Form>
  </Modal>;
}
