import { ArrowRightOutlined, DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Empty, message, Modal, Popconfirm, Space, Table, Tag } from "antd";
import type { Key } from "react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { RunStatusTag, runStatusLabel } from "../components/RunStatusTag";
import { api } from "../services/api";
import type { SolveRun } from "../types/api";

const formatTime = (value?: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";

export function RunsPage() {
  const client = useQueryClient();
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [page, setPage] = useState({ current: 1, pageSize: 10 });
  const query = useQuery({ queryKey: ["runs"], queryFn: api.listRuns });
  const runs = query.data ?? [];

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteRun(id),
    onSuccess: (_, id) => {
      setSelectedRowKeys((keys) => keys.filter((key) => key !== id));
      void client.invalidateQueries({ queryKey: ["runs"] });
      message.success("任务已删除");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const removeMany = useMutation({
    mutationFn: (ids: string[]) => api.deleteRuns(ids),
    onSuccess: (result) => {
      setSelectedRowKeys([]);
      setPage((value) => ({ ...value, current: 1 }));
      void client.invalidateQueries({ queryKey: ["runs"] });
      message.success(`已删除 ${result.deleted_count} 个解题任务`);
    },
    onError: (error: Error) => message.error(error.message),
  });

  const selectedIds = selectedRowKeys.map(String);
  const currentPageRuns = runs.slice(
    (page.current - 1) * page.pageSize,
    page.current * page.pageSize,
  );
  const currentPageIds = currentPageRuns.map((run) => run.id);

  const confirmBatchDelete = () => {
    if (!selectedIds.length) return;
    Modal.confirm({
      title: `确认删除 ${selectedIds.length} 个解题任务？`,
      content: "任务时间线、工具调用、证据和工作区都会被删除，且无法恢复。",
      okText: "批量删除",
      okType: "danger",
      cancelText: "取消",
      onOk: () => removeMany.mutate(selectedIds),
    });
  };

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>解题任务</h1>
          <p>查看每次自动化分析的状态、阶段、时间线与审计证据。</p>
        </div>
        <Space wrap>
          <Button onClick={() => setSelectedRowKeys(currentPageIds)} disabled={!currentPageIds.length}>
            全选当页
          </Button>
          <Button onClick={() => setSelectedRowKeys(runs.map((run) => run.id))} disabled={!runs.length}>
            全选全部
          </Button>
          <Button onClick={() => setSelectedRowKeys([])} disabled={!selectedIds.length}>
            清空选择
          </Button>
          <Button
            danger
            icon={<DeleteOutlined />}
            disabled={!selectedIds.length}
            loading={removeMany.isPending}
            onClick={confirmBatchDelete}
          >
            批量删除{selectedIds.length ? `（${selectedIds.length}）` : ""}
          </Button>
          <Button icon={<ReloadOutlined />} loading={query.isFetching} onClick={() => void query.refetch()}>
            刷新
          </Button>
        </Space>
      </div>
      <Card className="panel-card">
        <Table<SolveRun>
          className="cyber-table"
          rowKey="id"
          dataSource={runs}
          loading={query.isLoading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          pagination={{
            current: page.current,
            pageSize: page.pageSize,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 个任务`,
          }}
          onChange={(pagination) =>
            setPage({ current: pagination.current ?? 1, pageSize: pagination.pageSize ?? 10 })
          }
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
              title: "架构",
              dataIndex: "solver_mode",
              render: (mode: string) => (mode === "multi_agent_v1" ? "Multi-Agent v1" : "Single-Agent"),
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
                  {run.diagnostic_summary ? <Tag color="gold">{run.diagnostic_summary.slice(0, 18)}</Tag> : null}
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
                    <Button danger type="link" icon={<DeleteOutlined />} loading={remove.isPending || removeMany.isPending}>
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
