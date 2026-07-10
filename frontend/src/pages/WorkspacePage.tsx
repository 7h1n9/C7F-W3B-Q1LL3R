import { CaretRightOutlined, PauseCircleOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, Empty, Row, Space, Table, Timeline, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { RunStatusTag, runStatusLabel } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { RunEvent } from "../types/api";

const eventLabels: Record<string, string> = {
  "run.created": "任务已创建", "run.started": "任务已启动", "run.status_changed": "状态变更", "agent.message": "智能体消息", "agent.plan_created": "生成分析计划", "agent.hypothesis_created": "创建假设", "agent.hypothesis_updated": "更新假设", "tool.requested": "请求工具", "tool.started": "工具开始执行", "tool.output": "工具输出", "tool.completed": "工具执行完成", "tool.failed": "工具执行失败", "artifact.created": "保存证据文件", "flag.candidate_found": "发现 Flag 候选", "flag.verified": "Flag 已验证", "report.started": "开始生成报告", "report.completed": "报告生成完成", "run.completed": "任务完成", "run.failed": "任务失败",
};

function eventColor(type: string): string {
  if (type.includes("failed")) return "red";
  if (type.includes("completed") || type.includes("verified")) return "green";
  if (type.includes("tool")) return "blue";
  return "gray";
}

export function WorkspacePage() {
  const { id = "" } = useParams();
  const client = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const run = useQuery({ queryKey: ["run", id], queryFn: () => api.getRun(id) });
  const start = useMutation({ mutationFn: () => api.startRun(id), onSuccess: () => { void client.invalidateQueries({ queryKey: ["run", id] }); message.success("任务已提交启动"); }, onError: (error: Error) => message.error(error.message) });
  const cancel = useMutation({ mutationFn: () => api.cancelRun(id), onSuccess: () => { void client.invalidateQueries({ queryKey: ["run", id] }); message.success("任务已取消"); }, onError: (error: Error) => message.error(error.message) });
  useEffect(() => {
    // SSE 仅补充实时事件；服务端会先回放已持久化的历史记录。
    const source = api.streamRunEvents(id, (event) => setEvents((current) => current.some((item) => item.sequence === event.sequence) ? current : [...current, event]));
    return () => source.close();
  }, [id]);
  const auditEvents = useMemo(() => events.filter((event) => event.event_type.startsWith("tool.") || event.event_type.includes("hypothesis") || event.event_type.includes("artifact") || event.event_type.includes("flag")), [events]);

  return <>
    <div className="page-heading"><div><h1>解题工作区</h1><p>实时跟踪智能体分析过程、工具调用与可复核证据。</p></div><Space><Button type="primary" icon={<CaretRightOutlined />} loading={start.isPending} onClick={() => start.mutate()} disabled={run.data?.status !== "CREATED"}>启动任务</Button><Button danger icon={<PauseCircleOutlined />} loading={cancel.isPending} onClick={() => cancel.mutate()} disabled={!run.data || ["CANCELLED", "COMPLETED_SOLVED", "COMPLETED_UNSOLVED"].includes(run.data.status)}>取消任务</Button></Space></div>
    <Alert className="panel-card" showIcon type="info" icon={<SafetyCertificateOutlined />} message="安全边界已启用：仅允许访问题目配置中明确声明的主机与当前任务工作区。" />
    <Card className="panel-card" style={{ marginTop: 18 }}>
      <Descriptions column={{ xs: 1, md: 2, xl: 4 }} items={[
        { key: "id", label: "任务编号", children: <span className="id-code">{run.data?.id ?? "—"}</span> },
        { key: "status", label: "当前状态", children: run.data ? <RunStatusTag status={run.data.status} /> : "—" },
        { key: "engine", label: "解题引擎", children: run.data?.engine_type === "mock" ? "模拟演练引擎" : run.data?.engine_type },
        { key: "phase", label: "当前阶段", children: run.data ? runStatusLabel(run.data.current_phase) : "—" },
      ]} />
    </Card>
    <Row className="workspace-grid" gutter={[18, 18]}>
      <Col xs={24} xl={15}><Card className="panel-card" title="实时事件时间线"><Timeline items={events.length ? events.map((event) => ({ color: eventColor(event.event_type), children: <div><strong>{event.sequence.toString().padStart(3, "0")} · {eventLabels[event.event_type] ?? event.event_type}</strong><div className="event-payload">{event.created_at}　{JSON.stringify(event.payload_json)}</div></div> })) : [{ children: <Empty description="正在等待事件推送" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }]} /></Card></Col>
      <Col xs={24} xl={9}><Card className="panel-card" title="审计与证据"><Table className="cyber-table" size="small" rowKey="sequence" dataSource={auditEvents} columns={[{ title: "类型", dataIndex: "event_type", render: (type: string) => eventLabels[type] ?? type }, { title: "摘要", render: (_, event: RunEvent) => <span className="event-payload">{JSON.stringify(event.payload_json).slice(0, 110)}</span> }]} locale={{ emptyText: "暂无工具调用或证据记录" }} pagination={false} /></Card></Col>
    </Row>
  </>;
}
