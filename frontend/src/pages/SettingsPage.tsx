import { ApiOutlined, KeyOutlined, SafetyCertificateOutlined, ToolOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Col, Empty, Row, Table, Tag } from "antd";
import { api } from "../services/api";

export function SettingsPage() {
  const configs = useQuery({ queryKey: ["model-configs"], queryFn: api.listModelConfigs });
  return <>
    <div className="page-heading"><div><h1>系统配置</h1><p>查看模型接入配置与服务端安全边界。</p></div></div>
    <Alert className="panel-card" type="warning" showIcon icon={<KeyOutlined />} message="API 密钥为仅写入字段，前端不会读取或显示已保存的密钥明文。" />
    <Card className="panel-card" title="模型配置" style={{ marginTop: 18 }}>
      <Table className="cyber-table" rowKey="id" dataSource={configs.data} loading={configs.isLoading} locale={{ emptyText: <Empty description="尚未配置模型服务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
        { title: "名称", dataIndex: "name" }, { title: "提供方", dataIndex: "provider_type" }, { title: "服务地址", dataIndex: "base_url", ellipsis: true }, { title: "模型名称", dataIndex: "model_name" }, { title: "状态", dataIndex: "enabled", render: (enabled: boolean) => <Tag color={enabled ? "success" : "default"}>{enabled ? "已启用" : "已禁用"}</Tag> },
      ]} />
    </Card>
    <Row gutter={[18, 18]} style={{ marginTop: 18 }}>
      <Col xs={24} md={8}><Card className="panel-card" title={<><ApiOutlined /> OpenAI 兼容模型</>}>通过后端统一接入模型服务，支持基础超时与重试策略。</Card></Col>
      <Col xs={24} md={8}><Card className="panel-card" title={<><ToolOutlined /> Codex SDK 桥接服务</>}>仅在服务端运行。开发环境可使用模拟模式生成线程事件。</Card></Col>
      <Col xs={24} md={8}><Card className="panel-card" title={<><SafetyCertificateOutlined /> Kali 执行端</>}>工具调用必须经由工具网关，并受目标白名单与工作区边界限制。</Card></Col>
    </Row>
  </>;
}
