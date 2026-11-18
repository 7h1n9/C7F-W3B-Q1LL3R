import {
  CopyOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  SearchOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useMutation } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  message,
  Row,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { api } from "../services/api";
import type { KnowledgePreviewResponse } from "../types/api";

type PreviewResult = KnowledgePreviewResponse["results"][number];

interface PreviewFormValues {
  query: string;
  top_k: number;
  kind?: string[];
  technique?: string;
  target_stack?: string[];
  challenge_type?: string[];
}

const kindOptions = [
  { label: "technique", value: "technique" },
  { label: "case", value: "case" },
  { label: "poc", value: "poc" },
  { label: "negative", value: "negative" },
];

const challengeTypeOptions = [
  { label: "Web 靶场", value: "web" },
  { label: "流量分析", value: "traffic" },
  { label: "密码学", value: "crypto" },
  { label: "逆向", value: "reverse" },
  { label: "Pwn", value: "pwn" },
  { label: "杂项", value: "misc" },
];

function textList(values?: string[]) {
  if (!values?.length) {
    return <Typography.Text type="secondary">未记录</Typography.Text>;
  }
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      {values.map((value, index) => (
        <Typography.Text key={`${index}-${value}`} style={{ overflowWrap: "anywhere" }}>
          {value}
        </Typography.Text>
      ))}
    </Space>
  );
}

function ResultDetails({ result }: { result: PreviewResult }) {
  const { item } = result;
  return (
    <Collapse
      ghost
      size="small"
      items={[
        {
          key: "steps",
          label: `执行步骤 (${item.steps.length})`,
          children: textList(item.steps),
        },
        {
          key: "verification",
          label: `验证方式 (${item.verification_method.length})`,
          children: textList(item.verification_method),
        },
        {
          key: "failures",
          label: `已知失败 (${item.known_failures.length})`,
          children: textList(item.known_failures),
        },
        {
          key: "provenance",
          label: "来源与可信度",
          children: (
            <Descriptions
              size="small"
              column={{ xs: 1, sm: 2 }}
              items={[
                { key: "source_type", label: "来源类型", children: item.provenance.source_type },
                { key: "source_ref", label: "来源引用", children: item.provenance.source_ref },
                {
                  key: "source_url",
                  label: "来源链接",
                  children: item.provenance.source_url ? (
                    <Typography.Link
                      href={item.provenance.source_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {item.provenance.source_url}
                    </Typography.Link>
                  ) : (
                    "未记录"
                  ),
                },
                { key: "published_at", label: "发布时间", children: item.published_at ?? "未记录" },
                { key: "trust_level", label: "可信级别", children: item.trust_level },
                { key: "notes", label: "备注", children: item.provenance.notes ?? "未记录" },
              ]}
            />
          ),
        },
      ]}
    />
  );
}

function ResultCard({ result, rank }: { result: PreviewResult; rank: number }) {
  const { item } = result;
  return (
    <Card size="small" style={{ width: "100%" }}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Row gutter={[12, 10]} align="top">
          <Col flex="52px">
            <div
              style={{
                display: "grid",
                width: 42,
                height: 42,
                placeItems: "center",
                color: "#087d70",
                fontWeight: 800,
                background: "#e4fff8",
                border: "1px solid #a8eddf",
                borderRadius: 10,
              }}
            >
              #{rank}
            </div>
          </Col>
          <Col flex="auto">
            <Typography.Title level={5} style={{ margin: 0 }}>
              {item.title}
            </Typography.Title>
            <Space wrap size={[6, 6]} style={{ marginTop: 7 }}>
              <Typography.Text className="id-code">{item.id}</Typography.Text>
              <Tag color="cyan">{item.kind}</Tag>
              <Tag color={item.trust_level === "reviewed" ? "success" : "warning"}>
                {item.trust_level}
              </Tag>
              <Tag color="blue">score {result.score.toFixed(4)}</Tag>
            </Space>
          </Col>
        </Row>

        <Typography.Paragraph style={{ margin: 0, color: "#47726b" }}>
          {item.summary}
        </Typography.Paragraph>

        <Descriptions
          size="small"
          column={{ xs: 1, md: 2 }}
          items={[
            {
              key: "technique",
              label: "technique",
              children: <Typography.Text code>{item.technique}</Typography.Text>,
            },
            {
              key: "matched_terms",
              label: "matched_terms",
              children: result.matched_terms.length ? (
                <Space wrap size={[4, 4]}>
                  {result.matched_terms.map((term) => (
                    <Tag key={term}>{term}</Tag>
                  ))}
                </Space>
              ) : (
                "无"
              ),
            },
            {
              key: "target_stack",
              label: "target_stack",
              children: item.target_stack.length ? (
                <Space wrap size={[4, 4]}>
                  {item.target_stack.map((target) => (
                    <Tag color="geekblue" key={target}>
                      {target}
                    </Tag>
                  ))}
                </Space>
              ) : (
                "未记录"
              ),
            },
            {
              key: "preconditions",
              label: "适用条件",
              children: textList(item.preconditions),
            },
          ]}
        />

        <ResultDetails result={result} />
      </Space>
    </Card>
  );
}

export function KnowledgePreviewPage() {
  const [form] = Form.useForm<PreviewFormValues>();
  const preview = useMutation({
    mutationFn: (payload: {
      query: string;
      metadata: Record<string, unknown>;
      top_k: number;
    }) => api.previewKnowledge(payload),
    onError: (error: Error) => message.error(error.message),
  });

  const submit = (values: PreviewFormValues) => {
    if (preview.isPending) return;
    const metadata: Record<string, unknown> = {};
    if (values.kind?.length) metadata.kind = values.kind;
    if (values.technique?.trim()) metadata.technique = values.technique.trim();
    if (values.target_stack?.length) metadata.target_stack = values.target_stack;
    if (values.challenge_type?.length) metadata.challenge_type = values.challenge_type;

    preview.mutate({
      query: values.query.trim(),
      metadata,
      top_k: values.top_k,
    });
  };

  const copyPrior = async () => {
    if (!preview.data?.formatted_prior) return;
    try {
      await navigator.clipboard.writeText(preview.data.formatted_prior);
      message.success("PRIOR 已复制");
    } catch {
      message.error("复制失败，请检查浏览器剪贴板权限");
    }
  };

  const results = preview.data?.results ?? [];

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>知识检索预览</h1>
          <p>检查 BM25 召回、元数据匹配和最终注入模型的待验证 PRIOR。</p>
        </div>
        <Tag icon={<ExperimentOutlined />} color="cyan">
          stable ranking
        </Tag>
      </div>

      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        message="检索结果只是待验证 PRIOR"
        description="PRIOR 不是 VerifiedFact，也不能直接写入 Graph。所有知识只能用于提出假设，必须结合目标证据逐条验证。"
        style={{ marginBottom: 18 }}
      />

      <Card className="panel-card" title="检索条件" style={{ marginBottom: 18 }}>
        <Form<PreviewFormValues>
          form={form}
          layout="vertical"
          initialValues={{ top_k: 5, kind: [], target_stack: [], challenge_type: [] }}
          onFinish={submit}
        >
          <Row gutter={[14, 0]} align="bottom">
            <Col xs={24} lg={16}>
              <Form.Item
                name="query"
                label="查询内容"
                rules={[
                  { required: true, whitespace: true, message: "请输入查询内容" },
                  {
                    validator: async (_, value?: string) => {
                      if (value && !value.trim()) throw new Error("查询内容不能只包含空格");
                    },
                  },
                ]}
              >
                <Input
                  allowClear
                  prefix={<SearchOutlined />}
                  placeholder="例如：SQL injection boolean blind MySQL"
                />
              </Form.Item>
            </Col>
            <Col xs={12} lg={4}>
              <Form.Item
                name="top_k"
                label="返回数量"
                rules={[
                  { required: true, message: "请输入 top_k" },
                  { type: "number", min: 1, max: 100, message: "top_k 必须在 1 到 100 之间" },
                ]}
              >
                <InputNumber min={1} max={100} precision={0} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col xs={12} lg={4}>
              <Form.Item label=" ">
                <Button
                  block
                  type="primary"
                  htmlType="submit"
                  icon={<SearchOutlined />}
                  loading={preview.isPending}
                  disabled={preview.isPending}
                >
                  检索
                </Button>
              </Form.Item>
            </Col>
          </Row>

          <Divider orientation="left" plain>
            元数据过滤与排序增强
          </Divider>
          <Row gutter={[14, 0]}>
            <Col xs={24} md={12} xl={6}>
              <Form.Item name="kind" label="kind">
                <Select
                  mode="multiple"
                  allowClear
                  options={kindOptions}
                  placeholder="technique / case / poc / negative"
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12} xl={6}>
              <Form.Item name="technique" label="technique">
                <Input allowClear placeholder="例如：SQL injection" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12} xl={6}>
              <Form.Item name="target_stack" label="target_stack">
                <Select
                  mode="tags"
                  allowClear
                  tokenSeparators={[",", "，"]}
                  placeholder="输入后回车，如 mysql、spring"
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12} xl={6}>
              <Form.Item name="challenge_type" label="challenge_type">
                <Select
                  mode="multiple"
                  allowClear
                  options={challengeTypeOptions}
                  placeholder="选择题型"
                />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Card>

      {preview.isError ? (
        <Alert
          type="error"
          showIcon
          message="检索请求失败"
          description={(preview.error as Error).message}
          style={{ marginBottom: 18 }}
        />
      ) : null}

      {preview.data ? (
        <Space direction="vertical" size={18} style={{ width: "100%" }}>
          <Card
            className="panel-card"
            title={
              <Space>
                <DatabaseOutlined />
                <span>召回结果</span>
              </Space>
            }
            extra={
              <Space wrap>
                <Tag color="cyan">top_k={preview.data.top_k}</Tag>
                <Tag>{results.length} 条</Tag>
                <Tag color="green">score ↓ / query match ↓ / id ↑</Tag>
              </Space>
            }
          >
            {results.length ? (
              <List
                dataSource={results}
                renderItem={(result, index) => (
                  <List.Item style={{ display: "block", paddingInline: 0 }}>
                    <ResultCard result={result} rank={index + 1} />
                  </List.Item>
                )}
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="没有匹配的知识条目，请调整查询或放宽元数据条件"
              />
            )}
          </Card>

          <Card
            className="panel-card"
            title="formatted_prior"
            extra={
              <Button
                icon={<CopyOutlined />}
                onClick={() => void copyPrior()}
                disabled={!preview.data.formatted_prior}
              >
                复制
              </Button>
            }
          >
            <pre
              style={{
                maxHeight: 520,
                overflow: "auto",
                margin: 0,
                padding: 16,
                color: "#315f58",
                fontFamily: "Consolas, monospace",
                fontSize: 12,
                lineHeight: 1.65,
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                background: "#f4fffc",
                border: "1px solid #d5eee8",
                borderRadius: 10,
              }}
            >
              {preview.data.formatted_prior}
            </pre>
          </Card>
        </Space>
      ) : !preview.isError ? (
        <Card className="panel-card">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="提交检索条件后在这里查看稳定排序结果"
          />
        </Card>
      ) : null}
    </>
  );
}
