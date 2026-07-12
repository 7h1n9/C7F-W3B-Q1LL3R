import {
  CaretRightOutlined,
  PauseCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  Modal,
  Pagination,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Timeline,
  message,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { RunStatusTag, runStatusLabel } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { FlagCandidate, RunEvent } from "../types/api";

const eventLabels: Record<string, string> = {
  "run.created": "任务已创建",
  "run.started": "任务已启动",
  "run.restarted": "任务已重启",
  "run.status_changed": "状态变更",
  "agent.message": "智能体消息",
  "agent.plan_created": "生成分析计划",
  "agent.hypothesis_created": "创建假设",
  "agent.hypothesis_updated": "更新假设",
  "agent.action_requested": "请求动作",
  "agent.action_rejected": "动作被拒",
  "agent.action_completed": "动作完成",
  "agent.progress_detected": "检测到进展",
  "agent.no_progress": "暂无进展",
  "agent.replan_required": "需要重新规划",
  "skill.requested": "请求技能",
  "skill.snapshot_created": "技能快照已创建",
  "skill.activated": "Skill 已激活",
  "skill.deactivated": "Skill 已停用",
  "skill.recommended": "Skill 已推荐",
  "skill.activation_rejected": "Skill 激活失败",
  "tool.requested": "请求工具",
  "tool.started": "工具开始执行",
  "tool.output": "工具输出",
  "tool.completed": "工具执行完成",
  "tool.failed": "工具执行失败",
  "artifact.created": "保存证据文件",
  "flag.candidate_found": "发现 Flag 候选",
  "flag.reviewed": "Flag 已人工标记",
  "flag.verified": "Flag 已验证",
  "report.started": "开始生成报告",
  "report.completed": "报告生成完成",
  "run.completed": "任务完成",
  "run.failed": "任务失败",
};

function eventColor(type: string): string {
  if (type.includes("failed")) return "red";
  if (type.includes("completed") || type.includes("verified")) return "green";
  if (type.includes("tool") || type.includes("flag")) return "blue";
  if (type.includes("skill")) return "cyan";
  return "gray";
}

function flagStatusMeta(state?: FlagCandidate["review_state"]) {
  if (state === "VALID") return { color: "green", text: "正确" };
  if (state === "INVALID") return { color: "red", text: "错误" };
  return { color: "gold", text: "待确认" };
}

export function WorkspacePage() {
  const { id = "" } = useParams();
  const client = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [continuation, setContinuation] = useState("");
  const [artifactContent, setArtifactContent] = useState<{ path: string; content: string }>();
  const [timelinePage, setTimelinePage] = useState(1);
  const timelinePageSize = 12;

  const run = useQuery({ queryKey: ["run", id], queryFn: () => api.getRun(id) });
  const solverState = useQuery({ queryKey: ["solver-state", id], queryFn: () => api.getSolverState(id) });
  const diagnostics = useQuery({
    queryKey: ["run-diagnostics", id],
    queryFn: () => api.getRunDiagnostics(id),
  });
  const tools = useQuery({ queryKey: ["tool-calls", id], queryFn: () => api.getToolCalls(id) });
  const observations = useQuery({ queryKey: ["observations", id], queryFn: () => api.getObservations(id) });
  const artifacts = useQuery({ queryKey: ["artifacts", id], queryFn: () => api.getArtifacts(id) });
  const flags = useQuery({ queryKey: ["flags", id], queryFn: () => api.getFlags(id) });
  const report = useQuery({ queryKey: ["report", id], queryFn: () => api.getReport(id), retry: false });

  const start = useMutation({
    mutationFn: () => api.startRun(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["run", id] });
      message.success("任务已启动");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["run", id] });
      message.success("任务已取消");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const restart = useMutation({
    mutationFn: () => api.restartRun(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["run", id] });
      void client.invalidateQueries({ queryKey: ["solver-state", id] });
      void client.invalidateQueries({ queryKey: ["run-diagnostics", id] });
      void client.invalidateQueries({ queryKey: ["runs"] });
      message.success("任务已重启，将沿用原有状态与证据继续执行");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const reviewFlag = useMutation({
    mutationFn: (payload: { candidateId: string; reviewState: "OPEN" | "VALID" | "INVALID" }) =>
      api.reviewFlagCandidate(id, payload.candidateId, payload.reviewState),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["flags", id] });
            void client.invalidateQueries({ queryKey: ["run", id] });
      void client.invalidateQueries({ queryKey: ["runs"] });
      void client.invalidateQueries({ queryKey: ["report", id] });
      message.success("Flag 状态已更新");
    },
    onError: (error: Error) => message.error(error.message),
  });

  useEffect(() => {
    const source = api.streamRunEvents(id, (event) => {
      setEvents((current) =>
        current.some((item) => item.sequence === event.sequence) ? current : [...current, event],
      );
    });
    return () => source.close();
  }, [id]);

  const auditEvents = useMemo(
    () =>
      events.filter(
        (event) =>
          event.event_type.startsWith("tool.") ||
          event.event_type.startsWith("skill.") ||
          event.event_type.includes("hypothesis") ||
          event.event_type.includes("artifact") ||
          event.event_type.includes("flag"),
      ),
    [events],
  );

  const flagRows = flags.data ?? [];
  const timelineItems = useMemo(() => {
    const start = (timelinePage - 1) * timelinePageSize;
    return events.slice(start, start + timelinePageSize);
  }, [events, timelinePage]);

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(events.length / timelinePageSize));
    if (timelinePage > maxPage) setTimelinePage(maxPage);
  }, [events.length, timelinePage, timelinePageSize]);

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>解题工作区</h1>
          <p>实时跟踪智能体分析过程、工具调用与可复核证据。</p>
        </div>
        <Space>
          <Button
            type="primary"
            icon={<CaretRightOutlined />}
            loading={start.isPending}
            onClick={() => start.mutate()}
            disabled={run.data?.status !== "CREATED"}
          >
            启动任务
          </Button>
          <Button
            icon={<ReloadOutlined />}
            loading={restart.isPending}
            onClick={() => restart.mutate()}
            disabled={
              !run.data ||
              !["WAITING_USER", "FAILED_ENGINE", "FAILED_TOOL", "FAILED_RUNNER", "TIMEOUT", "COMPLETED_UNSOLVED", "CANCELLED"].includes(run.data.status)
            }
          >
            重启任务
          </Button>
          <Button
            danger
            icon={<PauseCircleOutlined />}
            loading={cancel.isPending}
            onClick={() => cancel.mutate()}
            disabled={!run.data || ["CANCELLED", "COMPLETED_SOLVED", "COMPLETED_UNSOLVED"].includes(run.data.status)}
          >
            取消任务
          </Button>
        </Space>
      </div>

      <Alert
        className="panel-card"
        showIcon
        type="info"
        icon={<SafetyCertificateOutlined />}
        message="安全边界已启用：仅允许访问题目配置中明确声明的主机与当前任务工作区。"
      />

      <Card className="panel-card" style={{ marginTop: 18 }}>
        <Descriptions
          column={{ xs: 1, md: 2, xl: 4 }}
          items={[
            { key: "id", label: "任务编号", children: <span className="id-code">{run.data?.id ?? "—"}</span> },
            { key: "challenge", label: "题目", children: run.data?.challenge_name ?? run.data?.challenge_id ?? "—" },
            { key: "status", label: "当前状态", children: run.data ? <RunStatusTag status={run.data.status} /> : "—" },
            {
              key: "engine",
              label: "解题引擎",
              children: run.data?.engine_type === "mock" ? "模拟引擎" : run.data?.engine_type,
            },
            { key: "model", label: "模型", children: run.data?.model_name ?? "—" },
            {
              key: "phase",
              label: "当前阶段",
              children: run.data ? runStatusLabel(run.data.current_phase) : "—",
            },
            {
              key: "skills",
              label: "活跃技能",
              children: (run.data?.active_skill_names ?? []).length ? (run.data?.active_skill_names ?? []).join("、") : "—",
            },
          ]}
        />
      </Card>

      <Card className="panel-card" title="方法论状态" style={{ marginTop: 18 }}>
        <Row gutter={[12, 12]}>
          <Col xs={24} md={8}>
            <Statistic title="当前阶段" value={solverState.data?.current_phase ?? run.data?.current_phase ?? "—"} />
          </Col>
          <Col xs={24} md={8}>
            <Statistic title="未取得进展次数" value={solverState.data?.no_progress_count ?? 0} />
          </Col>
          <Col xs={24} md={8}>
            <Statistic title="激活技能" value={solverState.data?.active_skill_ids_json.length ?? 0} />
          </Col>
        </Row>
        <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
          <Col xs={24} md={12}>
            <Card size="small" title="已确认事实" bordered={false}>
              <Space wrap>
                {(solverState.data?.confirmed_facts_json ?? []).length
                  ? (solverState.data?.confirmed_facts_json ?? []).map((item, index) => (
                      <Tag key={`${index}-${String(item.source ?? "fact")}`}>{String(item.source ?? "fact")}</Tag>
                    ))
                  : <span>暂无</span>}
              </Space>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card size="small" title="最近证据目标" bordered={false}>
              <Space direction="vertical" style={{ width: "100%" }}>
                {(solverState.data?.active_hypotheses_json ?? []).slice(0, 3).map((item) => (
                  <div key={String(item.id ?? item.statement ?? JSON.stringify(item))}>
                    {String(item.statement ?? item.title ?? "—")}
                  </div>
                ))}
                {!solverState.data?.active_hypotheses_json.length && <span>暂无</span>}
              </Space>
            </Card>
          </Col>
        </Row>
        <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
          <Col xs={24} md={12}>
            <Card size="small" title="已排除路径" bordered={false}>
              <Space wrap>
                {(solverState.data?.rejected_paths_json ?? []).length
                  ? (solverState.data?.rejected_paths_json ?? []).slice(0, 6).map((item, index) => (
                      <Tag key={`${index}-${String(item.tool ?? item.source ?? "reject")}`}>
                        {String(item.tool ?? item.source ?? "reject")}
                      </Tag>
                    ))
                  : <span>暂无</span>}
              </Space>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card size="small" title="活跃技能" bordered={false}>
              <Space wrap>
                {(solverState.data?.active_skill_ids_json ?? []).length
                  ? (solverState.data?.active_skill_ids_json ?? []).map((skillId) => (
                      <Tag key={skillId}>{skillId}</Tag>
                    ))
                  : <span>暂无</span>}
              </Space>
            </Card>
          </Col>
        </Row>
      </Card>

      <Card className="panel-card" title="异常诊断" style={{ marginTop: 18 }}>
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Space wrap>
            {(diagnostics.data?.diagnostic_tags ?? []).length ? (
              diagnostics.data?.diagnostic_tags?.map((tag) => <Tag key={tag} color="cyan">{tag}</Tag>)
            ) : (
              <Tag color="green">未见异常标签</Tag>
            )}
          </Space>
          <div className="field-help">{diagnostics.data?.diagnostic_summary ?? "暂无可用诊断"}</div>
          <Table
            className="cyber-table"
            size="small"
            rowKey="code"
            dataSource={diagnostics.data?.anomalies ?? []}
            columns={[
              { title: "代码", dataIndex: "code" },
              { title: "级别", dataIndex: "severity" },
              { title: "摘要", dataIndex: "summary" },
              { title: "恢复建议", dataIndex: "suggestion" },
            ]}
            locale={{ emptyText: "当前没有可识别的异常" }}
            pagination={{ pageSize: 4, showSizeChanger: false }}
          />
        </Space>
      </Card>

      <Row className="workspace-grid" gutter={[18, 18]}>
        <Col xs={24} xl={15}>
          <Card className="panel-card" title="实时事件时间线">
            <Timeline
              items={
                timelineItems.length
                  ? timelineItems.map((event) => ({
                      color: eventColor(event.event_type),
                      children: (
                        <div>
                          <strong>
                            {event.sequence.toString().padStart(3, "0")} ·{" "}
                            {eventLabels[event.event_type] ?? event.event_type}
                          </strong>
                          <div className="event-payload">
                            {event.created_at} · {JSON.stringify(event.payload_json)}
                          </div>
                        </div>
                      ),
                    }))
                  : [{ children: <Empty description="正在等待事件推送" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }]
              }
            />
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
              <Pagination
                size="small"
                current={timelinePage}
                total={events.length}
                pageSize={timelinePageSize}
                showSizeChanger={false}
                onChange={(page) => setTimelinePage(page)}
              />
            </div>
          </Card>
        </Col>
        <Col xs={24} xl={9}>
          <Card className="panel-card" title="审计与证据">
            <Table
              className="cyber-table"
              size="small"
              rowKey="sequence"
              dataSource={auditEvents}
              columns={[
                { title: "类型", dataIndex: "event_type", render: (type: string) => eventLabels[type] ?? type },
                {
                  title: "摘要",
                  render: (_, event: RunEvent) => (
                    <span className="event-payload">{JSON.stringify(event.payload_json).slice(0, 110)}</span>
                  ),
                },
              ]}
              locale={{ emptyText: "暂无工具调用或证据记录" }}
              pagination={{ pageSize: 6, showSizeChanger: false }}
            />
          </Card>
        </Col>
      </Row>

      <Card className="panel-card" title="工作区详情" style={{ marginTop: 18 }}>
        <Tabs
          items={[
            {
              key: "timeline",
              label: "时间线",
              children: (
                <Table
                  size="small"
                  rowKey="sequence"
                  dataSource={events}
                  columns={[
                    { title: "事件", dataIndex: "event_type" },
                    { title: "时间", dataIndex: "created_at" },
                  ]}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "agent",
              label: "Agent",
              children: (
                <Descriptions
                  column={1}
                  items={[
                    {
                      key: "steps",
                      label: "Agent 步数",
                      children: `${run.data?.agent_step_count ?? 0} / ${run.data?.max_agent_steps ?? 0}`,
                    },
                    { key: "error", label: "最近错误", children: run.data?.last_error_message ?? "—" },
                  ]}
                />
              ),
            },
            {
              key: "tools",
              label: "工具调用",
              children: (
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={tools.data ?? []}
                  columns={[
                    { title: "工具", dataIndex: "tool_name" },
                    { title: "状态", dataIndex: "status" },
                    {
                      title: "参数",
                      dataIndex: "arguments",
                      render: (value) => JSON.stringify(value),
                    },
                  ]}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "observations",
              label: "观察结果",
              children: (
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={observations.data ?? []}
                  columns={[
                    { title: "摘要", dataIndex: "summary" },
                    { title: "事实", dataIndex: "facts", render: (value) => JSON.stringify(value) },
                  ]}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "evidence",
              label: "证据",
              children: (
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={artifacts.data ?? []}
                  columns={[
                    { title: "路径", dataIndex: "path" },
                    { title: "摘要", dataIndex: "summary" },
                    { title: "大小", dataIndex: "size" },
                    {
                      title: "操作",
                      render: (_, item) => (
                        <Button
                          type="link"
                          onClick={() =>
                            api
                              .getArtifact(id, item.id)
                              .then(setArtifactContent)
                              .catch((error: Error) => message.error(error.message))
                          }
                        >
                          查看文本
                        </Button>
                      ),
                    },
                  ]}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "flag",
              label: "Flag",
              children: (
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={flagRows}
                  columns={[
                    { title: "候选", dataIndex: "candidate" },
                    {
                      title: "状态",
                      dataIndex: "review_state",
                      render: (value: FlagCandidate["review_state"]) => {
                        const meta = flagStatusMeta(value);
                        return <Tag color={meta.color}>{meta.text}</Tag>;
                      },
                    },
                    {
                      title: "自动判定",
                      dataIndex: "verified",
                      render: (value: boolean) => (value ? "是" : "否"),
                    },
                    {
                      title: "操作",
                      render: (_, item: FlagCandidate) => (
                        <Space size="small" wrap>
                          <Button
                            type="link"
                            onClick={() =>
                              reviewFlag.mutate({ candidateId: item.id, reviewState: "VALID" })
                            }
                          >
                            标记正确
                          </Button>
                          <Button
                            type="link"
                            danger
                            onClick={() =>
                              reviewFlag.mutate({ candidateId: item.id, reviewState: "INVALID" })
                            }
                          >
                            标记错误
                          </Button>
                          <Button
                            type="link"
                            onClick={() =>
                              reviewFlag.mutate({ candidateId: item.id, reviewState: "OPEN" })
                            }
                          >
                            重置待确认
                          </Button>
                        </Space>
                      ),
                    },
                  ]}
                  locale={{ emptyText: "暂无 Flag 候选" }}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "report",
              label: "报告",
              children: <pre className="event-payload">{report.data?.content ?? "报告尚未生成"}</pre>,
            },
          ]}
        />
      </Card>

      {run.data?.status === "WAITING_USER" && (
        <Card className="panel-card" title="补充信息" style={{ marginTop: 18 }}>
          <Space.Compact style={{ width: "100%" }}>
            <Input
              value={continuation}
              onChange={(event) => setContinuation(event.target.value)}
              placeholder="输入授权范围内的补充信息"
            />
            <Button
              type="primary"
              onClick={() =>
                api
                  .continueRun(id, continuation)
                  .then(() => {
                    setContinuation("");
                    message.success("已继续任务");
                  })
                  .catch((error: Error) => message.error(error.message))
              }
            >
              继续
            </Button>
          </Space.Compact>
        </Card>
      )}

      <Modal
        open={Boolean(artifactContent)}
        title={artifactContent?.path}
        footer={<Button onClick={() => setArtifactContent(undefined)}>关闭</Button>}
        onCancel={() => setArtifactContent(undefined)}
      >
        <pre className="event-payload">{artifactContent?.content}</pre>
      </Modal>
    </>
  );
}

