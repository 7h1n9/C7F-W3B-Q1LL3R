import {
  ApiOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlayCircleOutlined,
  ProfileOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Button, Layout, Menu, Tag } from "antd";
import { Link, Outlet, useLocation } from "react-router-dom";
import { useUiStore } from "../stores/ui";

const menuItems = [
  { key: "/", icon: <DashboardOutlined />, label: <Link to="/">态势总览</Link> },
  { key: "/challenges", icon: <ExperimentOutlined />, label: <Link to="/challenges">靶场题目</Link> },
  { key: "/skills", icon: <ProfileOutlined />, label: <Link to="/skills">Skill 管理</Link> },
  { key: "/runs", icon: <PlayCircleOutlined />, label: <Link to="/runs">解题任务</Link> },
  { key: "/settings", icon: <SettingOutlined />, label: <Link to="/settings">系统配置</Link> },
];

function selectedKey(pathname: string): string {
  if (pathname.startsWith("/skills")) return "/skills";
  if (pathname.startsWith("/challenges")) return "/challenges";
  if (pathname.startsWith("/runs")) return "/runs";
  if (pathname.startsWith("/settings")) return "/settings";
  return "/";
}

export function AppLayout() {
  const location = useLocation();
  const { collapsed, toggle } = useUiStore();

  return (
    <Layout className="app-shell">
      <Layout.Sider className="cyber-sider" collapsed={collapsed} collapsible trigger={null} width={252}>
        <div className="brand-lockup">
          <div className="brand-mark">C7</div>
          {!collapsed && <div><strong>C7F-W3B-Q1LL3R</strong><span>WEB 解题控制台</span></div>}
        </div>
        <Menu className="cyber-menu" mode="inline" selectedKeys={[selectedKey(location.pathname)]} items={menuItems} />
        {!collapsed && <div className="sider-footnote"><SafetyCertificateOutlined /> 仅限授权 CTF 与本地靶场</div>}
      </Layout.Sider>
      <Layout>
        <Layout.Header className="cyber-header">
          <Button className="collapse-button" type="text" onClick={toggle} icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} />
          <div className="header-context"><ApiOutlined /> 自动化解题控制台 <span>·</span> 实时审计已启用</div>
          <Tag className="status-chip" bordered={false}>授权模式</Tag>
        </Layout.Header>
        <Layout.Content className="cyber-content"><Outlet /></Layout.Content>
      </Layout>
    </Layout>
  );
}
