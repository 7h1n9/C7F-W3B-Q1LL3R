import {
  ClockCircleOutlined,
  DatabaseOutlined,
  ReloadOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";
import { api } from "../services/api";
import type {
  KnowledgeItem,
  KnowledgeListResponse,
} from "../types/api";

const kindOptions = [
  { label: "技术", value: "technique" },
  { label: "案例", value: "case" },
  { label: "PoC", value: "poc" },
  { label: "负向经验", value: "negative" },
];

const trustOptions = [
  { label: "Reviewed", value: "reviewed" },
  { label: "Curated", value: "curated" },
  { label: "Unverified", value: "unverified" },
];

const kindLabels: Record<string, string> = Object.fromEntries(
  kindOptions.map((option) => [option.value, option.label]),
);

const trustLabels: Record<string, string> = Object.fromEntries(
  trustOptions.map((option) => [option.value, option.label]),
);

const trustColors: Record<string, string> = {
  reviewed: "success",
  curated: "processing",
  unverified: "warning",
};

function numberFrom(
  source: Record<string, unknown> | undefined,
  keys: string[],
): number | undefined {
  if (!source) return undefined;
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function DetailList({ values }: { values: string[] }) {
  if (!values.length) return <Typography.Text type="secondary">未记录</Typography.Text>;
  return (
    <ul style={{ margin: 0, paddingInlineStart: 20 }}>
      {values.map((value, index) => (
        <li key={`${index}-${value}`} style={{ marginBottom: 6, lineHeight: 1.7 }}>
          {value}
        </li>
      ))}
    </ul>
  );
}

export function KnowledgePage() {
  const [queryText, setQueryText] = useState("");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string>();
  const [trustLevel, setTrustLevel] = useState<string>();
  const [targetStackText, setTargetStackText] = useState("");
  const [targetStack, setTargetStack] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [selected, setSelected] = useState<KnowledgeItem>();

  const itemsQuery = useQuery({
    queryKey: ["knowledge-items", query, kind, trustLevel, targetStack, page, pageSize],
    queryFn: () =>
      api.listKnowledgeItems({
        query: query || undefined,
        kind,
        trust_level: trustLevel,
        target_stack: targetStack || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      }),
  });
  const statsQuery = useQuery({
    queryKey: ["knowledge-stats"],
    queryFn: api.getKnowledgeStats,
  });

  const response = itemsQuery.data as
    | (KnowledgeListResponse & { entries?: KnowledgeItem[] })
    | undefined;
  const items = response?.items ?? response?.entries ?? [];
  const total = response?.total ?? items.length;
  const stats = statsQuery.data as unknown as Record<string, unknown> | undefined;
  const byTrust = stats?.by_trust_level as Record<string, unknown> | undefined;
  const statsTotal =
    numberFrom(stats, ["total", "item_count", "count"]) ?? total;
  const reviewed =
    numberFrom(byTrust, ["reviewed"]) ??
    numberFrom(stats, ["reviewed_count", "reviewed"]) ??
    0;
  const curated =
    numberFrom(byTrust, ["curated"]) ??
    numberFrom(stats, ["curated_count", "curated"]) ??
    0;
  const unverified =
    numberFrom(byTrust, ["unverified"]) ??
    numberFrom(stats, ["unverified_count", "unverified"]) ??
    0;
  const beforeCutoff =
    numberFrom(stats, [
      "published_before_2025_count",
      "published_before_2025_01_01",
      "published_before_cutoff",
      "eligible_count",
      "cutoff_count",
    ]) ?? statsTotal;

  const resetFilters = () => {
    setQueryText("");
    setQuery("");
    setKind(undefined);
    setTrustLevel(undefined);
    setTargetStackText("");
    setTargetStack("");
    setPage(1);
  };

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>知识库</h1>
          <p>浏览时间切分后的结构化安全先验，追溯来源并检查适用边界。</p>
        </div>
        <Tag color="warning" icon={<SafetyCertificateOutlined />}>
          待验证 PRIOR
        </Tag>
      </div>

      <Alert
        showIcon
        type="warning"
        message="知识是待验证 PRIOR，不是 VerifiedFact"
        description="这些条目只能帮助提出假设，不能替代目标上的可复现证据；它们不会进入 Graph 或被升级为已确认事实。"
        style={{ marginBottom: 18 }}
      />

      <Row gutter={[14, 14]} style={{ marginBottom: 18 }}>
        <Col xs={24} sm={12} xl={6}>
          <Card className="metric-card">
            <Statistic title="条目总数" value={statsTotal} prefix={<DatabaseOutlined />} />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Card className="metric-card">
            <Statistic
              title="时间切分：2025-01-01 前"
              value={beforeCutoff}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={4}>
          <Card className="metric-card">
            <Statistic title="Reviewed" value={reviewed} />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={4}>
          <Card className="metric-card">
            <Statistic title="Curated" value={curated} />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={4}>
          <Card className="metric-card">
            <Statistic title="Unverified" value={unverified} />
          </Card>
        </Col>
      </Row>

      <Card className="panel-card" title="知识条目">
        <Space
          wrap
          size={[10, 10]}
          style={{ width: "100%", marginBottom: 18 }}
        >
          <Input.Search
            allowClear
            value={queryText}
            placeholder="搜索标题、技术、摘要或 ID"
            prefix={<SearchOutlined />}
            style={{ width: "min(360px, 100%)" }}
            enterButton="搜索"
            onChange={(event) => {
              setQueryText(event.target.value);
              if (!event.target.value) {
                setQuery("");
                setPage(1);
              }
            }}
            onSearch={(value) => {
              setQuery(value.trim());
              setPage(1);
            }}
          />
          <Select
            allowClear
            value={kind}
            options={kindOptions}
            placeholder="类型"
            style={{ width: 150 }}
            onChange={(value) => {
              setKind(value);
              setPage(1);
            }}
          />
          <Select
            allowClear
            value={trustLevel}
            options={trustOptions}
            placeholder="Trust level"
            style={{ width: 160 }}
            onChange={(value) => {
              setTrustLevel(value);
              setPage(1);
            }}
          />
          <Input.Search
            allowClear
            value={targetStackText}
            placeholder="适用栈，如 Java、MySQL"
            style={{ width: 230 }}
            enterButton="筛选"
            onChange={(event) => {
              setTargetStackText(event.target.value);
              if (!event.target.value) {
                setTargetStack("");
                setPage(1);
              }
            }}
            onSearch={(value) => {
              setTargetStack(value.trim());
              setPage(1);
            }}
          />
          <Button icon={<ReloadOutlined />} onClick={resetFilters}>
            重置
          </Button>
        </Space>

        {itemsQuery.isError && (
          <Alert
            showIcon
            type="error"
            message="知识条目加载失败"
            description={(itemsQuery.error as Error).message}
            style={{ marginBottom: 14 }}
          />
        )}

        <Table
          className="cyber-table"
          rowKey="id"
          loading={itemsQuery.isLoading || itemsQuery.isFetching}
          dataSource={items}
          locale={{
            emptyText: (
              <Empty
                description="没有符合筛选条件的知识条目"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
          scroll={{ x: 1120 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (count) => `共 ${count} 条`,
          }}
          onChange={(pagination) => {
            setPage(pagination.current ?? 1);
            setPageSize(pagination.pageSize ?? 10);
          }}
          onRow={(record) => ({
            onClick: () => setSelected(record),
            style: { cursor: "pointer" },
          })}
          columns={[
            {
              title: "标题 / ID",
              dataIndex: "title",
              width: 280,
              render: (_: unknown, row: KnowledgeItem) => (
                <div>
                  <strong>{row.title}</strong>
                  <div className="id-code">{row.id}</div>
                </div>
              ),
            },
            {
              title: "类型",
              dataIndex: "kind",
              width: 96,
              render: (value: string) => (
                <Tag color={value === "poc" ? "geekblue" : value === "negative" ? "error" : "cyan"}>
                  {kindLabels[value] ?? value}
                </Tag>
              ),
            },
            {
              title: "技术",
              dataIndex: "technique",
              width: 210,
              ellipsis: true,
            },
            {
              title: "适用栈",
              dataIndex: "target_stack",
              width: 240,
              render: (values: string[]) => (
                <Space size={[4, 4]} wrap>
                  {values.map((value) => (
                    <Tag key={value}>{value}</Tag>
                  ))}
                </Space>
              ),
            },
            {
              title: "Trust level",
              dataIndex: "trust_level",
              width: 126,
              render: (value: string) => (
                <Tag color={trustColors[value] ?? "default"}>
                  {trustLabels[value] ?? value}
                </Tag>
              ),
            },
            {
              title: "发布时间",
              dataIndex: "published_at",
              width: 116,
              render: (value?: string | null) => value ?? "未记录",
            },
            {
              title: "来源 ref",
              width: 280,
              render: (_: unknown, row: KnowledgeItem) => (
                <Typography.Text ellipsis={{ tooltip: row.provenance.source_ref }}>
                  {row.provenance.source_ref}
                </Typography.Text>
              ),
            },
          ]}
        />
      </Card>

      <Drawer
        open={Boolean(selected)}
        width="min(760px, 100vw)"
        destroyOnClose
        onClose={() => setSelected(undefined)}
        title={
          selected ? (
            <div>
              <div>{selected.title}</div>
              <div className="id-code" style={{ marginTop: 5 }}>
                {selected.id}
              </div>
            </div>
          ) : null
        }
        extra={<Tag color="warning">待验证 PRIOR</Tag>}
      >
        {selected && (
          <Space direction="vertical" size={20} style={{ width: "100%" }}>
            <Alert
              showIcon
              type="warning"
              message="本条知识不是 VerifiedFact"
              description="使用前必须在授权目标上按验证方法复现；已知失败条件同样需要重新检查。"
            />

            <Descriptions
              bordered
              size="small"
              column={{ xs: 1, sm: 1, md: 2 }}
              items={[
                {
                  key: "kind",
                  label: "类型",
                  children: kindLabels[selected.kind] ?? selected.kind,
                },
                {
                  key: "trust",
                  label: "Trust level",
                  children: (
                    <Tag color={trustColors[selected.trust_level]}>
                      {trustLabels[selected.trust_level] ?? selected.trust_level}
                    </Tag>
                  ),
                },
                {
                  key: "technique",
                  label: "技术",
                  children: selected.technique,
                },
                {
                  key: "published",
                  label: "发布时间",
                  children: selected.published_at ?? "未记录",
                },
                {
                  key: "stack",
                  label: "适用栈",
                  children: (
                    <Space size={[4, 4]} wrap>
                      {selected.target_stack.map((value) => (
                        <Tag key={value}>{value}</Tag>
                      ))}
                    </Space>
                  ),
                },
                {
                  key: "source-type",
                  label: "来源类型",
                  children: selected.provenance.source_type,
                },
              ]}
            />

            <section>
              <Typography.Title level={5}>摘要</Typography.Title>
              <Typography.Paragraph>{selected.summary}</Typography.Paragraph>
            </section>

            <section>
              <Typography.Title level={5}>前置条件</Typography.Title>
              <DetailList values={selected.preconditions} />
            </section>

            <section>
              <Typography.Title level={5}>步骤</Typography.Title>
              <DetailList values={selected.steps} />
            </section>

            <section>
              <Typography.Title level={5}>Payload template</Typography.Title>
              <pre
                style={{
                  margin: 0,
                  padding: 12,
                  overflowX: "auto",
                  color: "#235b54",
                  background: "#f3fbf8",
                  border: "1px solid #d8eee9",
                  borderRadius: 10,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {selected.payload_template ?? "未提供"}
              </pre>
            </section>

            <section>
              <Typography.Title level={5}>验证方式</Typography.Title>
              <DetailList values={selected.verification_method} />
            </section>

            <section>
              <Typography.Title level={5}>已知失败 / 限制</Typography.Title>
              <DetailList values={selected.known_failures} />
            </section>

            <section>
              <Typography.Title level={5}>Provenance</Typography.Title>
              <Descriptions
                bordered
                size="small"
                column={1}
                items={[
                  {
                    key: "source-ref",
                    label: "Source ref",
                    children: selected.provenance.source_ref,
                  },
                  {
                    key: "source-url",
                    label: "Source URL",
                    children: selected.provenance.source_url ? (
                      <a
                        href={selected.provenance.source_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {selected.provenance.source_url}
                      </a>
                    ) : (
                      "未记录"
                    ),
                  },
                  {
                    key: "notes",
                    label: "Notes",
                    children: selected.provenance.notes ?? "未记录",
                  },
                ]}
              />
            </section>
          </Space>
        )}
      </Drawer>
    </>
  );
}
