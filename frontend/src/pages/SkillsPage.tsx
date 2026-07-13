import { CheckOutlined, CopyOutlined, PlusOutlined, StopOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, message, Space, Table, Tag } from "antd";
import { useState } from "react";
import { SkillEditor } from "../components/skills/SkillEditor";
import { api } from "../services/api";
import type { Skill } from "../types/api";

export function SkillsPage() {
  const client = useQueryClient();
  const [editing, setEditing] = useState<Skill>();
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  const candidates = useQuery({ queryKey: ["learned-skill-candidates"], queryFn: api.listLearnedSkillCandidates });
  const save = useMutation({ mutationFn: async (payload: Record<string, unknown>) => editing ? api.updateSkill(editing.id, payload) : api.createSkill(payload), onSuccess: () => { void client.invalidateQueries({ queryKey: ["skills"] }); setOpen(false); message.success("Skill 已保存"); }, onError: (error: Error) => message.error(error.message) });
  const duplicate = useMutation({ mutationFn: api.duplicateSkill, onSuccess: () => { void client.invalidateQueries({ queryKey: ["skills"] }); message.success("已创建自定义副本"); }, onError: (error: Error) => message.error(error.message) });
  const review = useMutation({ mutationFn: ({ id, decision }: { id: string; decision: "APPROVE" | "REJECT" }) => api.reviewLearnedSkillCandidate(id, decision), onSuccess: () => { void client.invalidateQueries({ queryKey: ["learned-skill-candidates"] }); message.success("候选审核状态已更新"); }, onError: (error: Error) => message.error(error.message) });

  return <>
    <div className="page-heading"><div><h1>Skill 管理</h1><p>管理内置方法论、专项 Skill 与经过隔离审核的成功经验候选。</p></div><Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditing(undefined); setOpen(true); }}>新建 Skill</Button></div>
    <Card className="panel-card"><Table rowKey="id" loading={query.isLoading} dataSource={query.data} columns={[
      { title: "名称", render: (_, row: Skill) => <Space><strong>{row.display_name}</strong>{row.source_type === "BUILTIN" && <Tag color="cyan">内置</Tag>}</Space> },
      { title: "分类", dataIndex: "skill_kind" },
      { title: "适用题型", dataIndex: "challenge_types", render: (types: string[]) => types.map(type => <Tag key={type}>{type === "WEB_TARGET" ? "Web" : "流量"}</Tag>) },
      { title: "目录", dataIndex: "catalog_scope" }, { title: "版本", dataIndex: "version" },
      { title: "状态", dataIndex: "enabled", render: (value: boolean) => <Tag color={value ? "success" : "default"}>{value ? "启用" : "停用"}</Tag> },
      { title: "操作", render: (_, row: Skill) => <Space><Button type="link" onClick={() => { setEditing(row); setOpen(true); }}>{row.source_type === "BUILTIN" ? "查看" : "编辑"}</Button>{row.source_type === "BUILTIN" && <Button type="link" icon={<CopyOutlined />} onClick={() => duplicate.mutate(row.id)}>复制为自定义</Button>}</Space> },
    ]} /></Card>
    <Card className="panel-card" title="成功经验候选（隔离区）"><Table rowKey="id" loading={candidates.isLoading} dataSource={candidates.data} columns={[
      { title: "中文标题", dataIndex: "display_name" }, { title: "来源 Run", dataIndex: "source_run_id" },
      { title: "状态", dataIndex: "status", render: (value: string) => <Tag color={value === "APPROVED" ? "success" : value === "REJECTED" ? "error" : "warning"}>{value}</Tag> },
      { title: "污染扫描", render: (_, row: Record<string, unknown>) => (row.security_scan as { passed?: boolean } | undefined)?.passed ? "通过" : "需复核" },
      { title: "审核", render: (_, row: Record<string, unknown>) => <Space><Button size="small" icon={<CheckOutlined />} disabled={row.status !== "QUARANTINED" && row.status !== "REVIEW_REQUIRED"} onClick={() => review.mutate({ id: String(row.id), decision: "APPROVE" })}>批准</Button><Button size="small" danger icon={<StopOutlined />} disabled={row.status === "REJECTED"} onClick={() => review.mutate({ id: String(row.id), decision: "REJECT" })}>拒绝</Button></Space> },
    ]} /></Card>
    <SkillEditor open={open} value={editing} loading={save.isPending} onClose={() => setOpen(false)} onSave={value => save.mutate(value)} />
  </>;
}
