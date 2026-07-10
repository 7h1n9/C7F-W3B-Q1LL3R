import { ArrowRightOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Empty, Table } from "antd";
import { Link } from "react-router-dom";
import { RunStatusTag, runStatusLabel } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { SolveRun } from "../types/api";

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";

export function RunsPage() {
  const query = useQuery({ queryKey: ["runs"], queryFn: api.listRuns });
  return <>
    <div className="page-heading"><div><h1>解题任务</h1><p>查看每次自动化分析的状态、阶段、时间线与审计证据。</p></div></div>
    <Card className="panel-card">
      <Table<SolveRun> className="cyber-table" rowKey="id" dataSource={query.data} loading={query.isLoading} locale={{ emptyText: <Empty description="尚未创建解题任务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
        { title: "任务编号", dataIndex: "id", render: (id: string) => <span className="id-code">{id.slice(0, 8)}</span> },
        { title: "题目编号", dataIndex: "challenge_id", render: (id: string) => <span className="id-code">{id.slice(0, 8)}</span> },
        { title: "引擎", dataIndex: "engine_type", render: (engine: string) => engine === "mock" ? "模拟演练" : engine },
        { title: "状态", dataIndex: "status", render: (status: string) => <RunStatusTag status={status} /> },
        { title: "当前阶段", dataIndex: "current_phase", render: runStatusLabel },
        { title: "启动时间", dataIndex: "started_at", render: formatTime },
        { title: "结束时间", dataIndex: "finished_at", render: formatTime },
        { title: "操作", render: (_, run) => <Link to={`/runs/${run.id}`}><Button type="link" icon={<ArrowRightOutlined />}>进入工作区</Button></Link> },
      ]} />
    </Card>
  </>;
}
