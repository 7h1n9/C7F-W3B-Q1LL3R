import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "antd/dist/reset.css";
import { AppLayout } from "./layouts/AppLayout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ArenaPage } from "./pages/ArenaPage";
import { ChallengesPage } from "./pages/ChallengesPage";
import { DashboardPage } from "./pages/DashboardPage";
import { KnowledgeExperimentsPage } from "./pages/KnowledgeExperimentsPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { KnowledgePreviewPage } from "./pages/KnowledgePreviewPage";
import { RunsPage } from "./pages/RunsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SkillsPage } from "./pages/SkillsPage";
import { WorkspacePage } from "./pages/WorkspacePage";
import "./styles/global.css";

// 统一管理服务端数据缓存，避免页面切换时重复请求。
const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <ConfigProvider locale={zhCN}
    theme={{
      token: {
        colorPrimary: "#00c9a7",
        colorInfo: "#00c9a7",
        colorSuccess: "#14b8a6",
        colorWarning: "#f6b94f",
        colorError: "#fb7185",
        borderRadius: 10,
        fontFamily: '"Microsoft YaHei", "PingFang SC", sans-serif',
      },
    }}
  >
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/challenges" element={<ChallengesPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:id" element={<WorkspacePage />} />
            <Route path="/arena" element={<ArenaPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/knowledge/preview" element={<KnowledgePreviewPage />} />
            <Route path="/knowledge/experiments" element={<KnowledgeExperimentsPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </ConfigProvider>,
);
