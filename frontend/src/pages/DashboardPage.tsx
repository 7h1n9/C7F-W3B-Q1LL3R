import { CheckCircleOutlined, ExperimentOutlined, PlayCircleOutlined, RadarChartOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Card, Col, Empty, Row, Statistic, Table } from "antd";
import { Link } from "react-router-dom";
import { RunStatusTag } from "../components/RunStatusTag";
import { api } from "../services/api";

export function DashboardPage() {
  const challenges = useQuery({ queryKey: ["challenges"], queryFn: api.listChallenges });
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.listRuns });
  const allRuns = runs.data ?? [];
  const activeRuns = allRuns.filter((run) => !["CREATED", "CANCELLED"].includes(run.status) && !run.status.startsWith("COMPLETED") && !run.status.startsWith("FAILED"));

  return <>
    <div className="page-heading"><div><h1>态势总览</h1><p>授权 Web CTF 解题任务的实时运行态势与审计入口。</p></div></div>
    <Row gutter={[18, 18]}>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="靶场题目" value={challenges.data?.length ?? 0} prefix={<ExperimentOutlined />} /></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="解题任务" value={allRuns.length} prefix={<PlayCircleOutlined />} /></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="正在运行" value={activeRuns.length} prefix={<RadarChartOutlined />} /></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card className="metric-card"><Statistic title="成功解出" value={allRuns.filter((run) => run.status === "COMPLETED_SOLVED").length} prefix={<CheckCircleOutlined />} /></Card></Col>
    </Row>
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
