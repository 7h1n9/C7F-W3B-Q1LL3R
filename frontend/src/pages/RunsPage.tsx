import { ArrowRightOutlined, DeleteOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Empty, message, Popconfirm, Space, Table, Tag } from "antd";
import { Link } from "react-router-dom";
import { RunStatusTag, runStatusLabel } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { SolveRun } from "../types/api";

const formatTime = (value?: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";

export function RunsPage() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["runs"], queryFn: api.listRuns });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteRun(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["runs"] });
      message.success("任务已删除");
    },
    onError: (error: Error) => message.error(error.message),
  });

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>解题任务</h1>
          <p>查看每次自动化分析的状态、阶段、时间线与审计证据。</p>
        </div>
      </div>
      <Card className="panel-card">
        <Table<SolveRun>
          className="cyber-table"
          rowKey="id"
          dataSource={query.data}
          loading={query.isLoading}
          locale={{ emptyText: <Empty description="尚未创建解题任务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          columns={[
            {
              title: "任务编号",
              dataIndex: "id",
              render: (id: string) => <span className="id-code">{id.slice(0, 8)}</span>,
            },
            {
              title: "题目",
              render: (_, run) =>
                run.challenge_name ? (
                  <Space direction="vertical" size={0}>
                    <span>{run.challenge_name}</span>
                    <span className="id-code">{run.challenge_id.slice(0, 8)}</span>
                  </Space>
                ) : (
                  <span className="id-code">{run.challenge_id.slice(0, 8)}</span>
                ),
            },
            {
              title: "题型",
              dataIndex: "challenge_type",
              render: (value: string | undefined) =>
                value === "TRAFFIC_ANALYSIS" ? "流量分析" : value === "WEB_TARGET" ? "Web 靶场" : "—",
            },
            {
              title: "引擎",
              dataIndex: "engine_type",
              render: (engine: string) => (engine === "mock" ? "模拟引擎" : engine),
            },
            {
              title: "状态",
              dataIndex: "status",
              render: (status: string) => <RunStatusTag status={status} />,
            },
            { title: "当前阶段", dataIndex: "current_phase", render: runStatusLabel },
            {
              title: "技能 / 诊断",
              render: (_, run) => (
                <Space wrap size={[4, 4]}>
                  {(run.active_skill_names ?? []).slice(0, 2).map((name) => (
                    <Tag key={`${run.id}-${name}`}>{name}</Tag>
                  ))}
                  {(run.diagnostic_tags ?? []).slice(0, 2).map((tag) => (
                    <Tag key={`${run.id}-${tag}`} color="cyan">
                      {tag}
                    </Tag>
                  ))}
                  {run.diagnostic_summary ? (
                    <Tag color="gold">{run.diagnostic_summary.slice(0, 18)}</Tag>
                  ) : null}
                </Space>
              ),
            },
            { title: "启动时间", dataIndex: "started_at", render: formatTime },
            { title: "结束时间", dataIndex: "finished_at", render: formatTime },
            {
              title: "操作",
              render: (_, run) => (
                <Space>
                  <Link to={`/runs/${run.id}`}>
                    <Button type="link" icon={<ArrowRightOutlined />}>
                      进入工作区
                    </Button>
                  </Link>
                  <Popconfirm
                    title="确认删除这个解题任务？"
                    description="任务时间线、工具调用、证据和本地工作区都会被删除。"
                    onConfirm={() => remove.mutate(run.id)}
                    okText="删除"
                    cancelText="取消"
                  >
                    <Button danger type="link" icon={<DeleteOutlined />} loading={remove.isPending}>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </>
  );
}
