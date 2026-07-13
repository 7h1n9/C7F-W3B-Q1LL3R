import { CheckCircleFilled, CheckCircleOutlined, ExperimentOutlined, PlayCircleOutlined, RadarChartOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Card, Col, Empty, Row, Statistic, Table, Tag } from "antd";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { RunStatusTag } from "../components/RunStatusTag";
import { api } from "../services/api";

const readinessLevelLabels: Record<string, string> = {
  NOT_READY: "尚未就绪",
  READY_FOR_WEB_SMOKE_TEST: "已具备 Web 冒烟测试条件",
  READY_FOR_TRAFFIC_SMOKE_TEST: "已具备流量冒烟测试条件",
  READY_FOR_RANGE_SMOKE_TEST: "已具备靶场冒烟测试条件",
};

const checkNameLabels: Record<string, string> = {
  database: "数据库",
  runner: "Runner",
  runner_token: "Runner Token",
  web_tool: "Web 工具",
  tshark: "tshark",
  capinfos: "capinfos",
  model_config: "模型配置",
  skills: "Skills",
  pcap_upload_validation: "PCAP 上传校验",
};

function readinessMessage(name: string, message: string): string {
  if (name === "database") return message.replace("Migration revision:", "迁移版本：");
  if (message === "Runner is reachable") return "Runner 可达";
  if (message === "Runner client token is configured") return "Runner Token 已配置";
  if (message === "Runner reports a healthy execution backend") return "Runner 执行后端正常";
  if (message === "tshark capability") return "tshark 可用";
  if (message === "capinfos capability") return "capinfos 可用";
  if (message === "PCAP extension and magic validation is installed") return "PCAP 扩展名与文件魔数校验已启用";
  return message
    .replace(/enabled model configuration\(s\); connection tests are performed from Settings\./, "个模型配置已启用；可在系统配置中执行连接测试。")
    .replace(/enabled Skill\(s\)/, "个 Skill 已启用");
}

export function DashboardPage() {
  const challenges = useQuery({ queryKey: ["challenges"], queryFn: api.listChallenges });
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.listRuns });
  const readiness = useQuery({ queryKey: ["range-readiness"], queryFn: api.getReadiness, refetchInterval: 30000 });
  const allRuns = runs.data ?? [];
  const activeRuns = allRuns.filter((run) => !["CREATED", "CANCELLED"].includes(run.status) && !run.status.startsWith("COMPLETED") && !run.status.startsWith("FAILED"));
  const checks = readiness.data?.checks ?? [];
  const healthyChecks = checks.filter((check) => check.ok).length;
  const readinessPercent = checks.length ? Math.round((healthyChecks / checks.length) * 100) : 0;
  const readinessTitle = readiness.data ? readinessLevelLabels[readiness.data.level] ?? "尚未就绪" : "正在检查…";

  return <>
    <div className="page-heading"><div><h1>态势总览</h1><p>授权 Web CTF 解题任务的实时运行态势与审计入口。</p></div></div>
    <Row gutter={[18, 18]}>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="靶场题目" value={challenges.data?.length ?? 0} prefix={<ExperimentOutlined />} /></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="解题任务" value={allRuns.length} prefix={<PlayCircleOutlined />} /></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="正在运行" value={activeRuns.length} prefix={<RadarChartOutlined />} /></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="成功解出" value={allRuns.filter((run) => run.status === "COMPLETED_SOLVED").length} prefix={<CheckCircleOutlined />} /></Card></Col>
    </Row>
    <Card className="panel-card readiness-card" title={<div className="readiness-card-title"><div><span className="readiness-eyebrow">RANGE OPS / SYSTEM CHECK</span><span className="readiness-heading">靶场测试就绪度</span></div><Tag className={readiness.data?.ready ? "readiness-tag is-ready" : "readiness-tag"} icon={<SafetyCertificateOutlined />}>{readiness.data?.ready ? "系统正常" : "需要关注"}</Tag></div>} style={{ marginTop: 22 }}>
      <div className="readiness-summary">
        <div className="readiness-score">
          <div className="readiness-chart-wrap">
            <div
              className={`readiness-ring ${readiness.data?.ready ? "is-ready" : ""}`}
              style={{ "--readiness-angle": `${readinessPercent * 3.6}deg` } as CSSProperties}
              aria-label={`${healthyChecks} / ${checks.length || 0} 项检查通过`}
            >
              <div className="readiness-ring-core"><strong>{healthyChecks}</strong><span>/{checks.length || "—"}</span><small>PASS</small></div>
            </div>
            <span className="readiness-float readiness-float-top">{readinessPercent}%</span>
            <span className="readiness-float readiness-float-bottom">{checks.length} 项检查</span>
          </div>
          <div><div className="readiness-status">{readinessTitle}</div><p>{readiness.isLoading ? "正在检查数据库、Runner 与工具能力…" : `${healthyChecks} / ${checks.length} 项检查通过`}</p></div>
        </div>
        <div className="readiness-progress"><div className="readiness-progress-meta"><span>系统能力覆盖率</span><strong>{readinessPercent}%</strong></div><div className="readiness-progress-track"><span style={{ width: `${readinessPercent}%` }} /></div><small>每 30 秒自动刷新一次</small></div>
      </div>
      <div className="readiness-check-grid">{checks.map((check) => <div className={`readiness-check ${check.ok ? "is-ok" : "is-blocked"}`} key={check.name}><div className="readiness-check-icon">{check.ok ? <CheckCircleFilled /> : <span>!</span>}</div><div className="readiness-check-copy"><strong>{checkNameLabels[check.name] ?? check.name}</strong><span>{readinessMessage(check.name, check.message)}</span></div><Tag>{check.ok ? "正常" : "阻塞"}</Tag></div>)}</div>
    </Card>
    <Card className="panel-card" title="最近解题任务" extra={<Link to="/runs">查看全部</Link>} style={{ marginTop: 22 }}>
      <Table className="cyber-table" rowKey="id" dataSource={allRuns.slice(0, 8)} locale={{ emptyText: <Empty description="尚未创建解题任务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
        { title: "任务编号", dataIndex: "id", render: (id: string) => <span className="id-code">{id.slice(0, 8)}</span> },
        { title: "引擎", dataIndex: "engine_type", render: (engine: string) => engine === "mock" ? "模拟演练引擎" : engine },
        { title: "当前状态", dataIndex: "status", render: (status: string) => <RunStatusTag status={status} /> },
        { title: "阶段", dataIndex: "current_phase" },
        { title: "操作", render: (_, run) => <Link to={`/runs/${run.id}`}>进入工作区</Link> },
      ]} pagination={false} />
    </Card>
  </>;
}
