# C7F-W3B-Q1LL3R

> CTF Web Agent 初版基础架构

这是一个面向授权场景的 CTF Web 解题工作流单仓库。它只用于本地练习靶场、CTF 比赛，以及明确授权的测试场景。它不会提供任意 shell 命令执行、公共目标自动化、持久化、自动化利用、宽泛扫描或自动化 payload 库。

## 组件

- `backend/`：FastAPI、SQLAlchemy 2 异步数据访问、Alembic、状态机、Tool Gateway、SSE 持久化。
- `frontend/`：Vite + React + TypeScript + Ant Design 页面，包含总览、题目、任务、工作区和设置。
- `codex-bridge/`：仅服务端的 Fastify 桥接服务，用于 `@openai/codex-sdk`，支持确定性的 mock 模式。
- `kali-runner/`：独立的 FastAPI 服务，用于受限 HTTP、文件读取、文件搜索，以及对现有 Python 脚本的执行。
- `configs/`：原始 YAML 定义，包含一个安全的 Web-CTF 角色、四个工具和一个技能。

## 环境要求

- Python 3.11+
- Node.js 20+（Codex SDK 本身支持 Node 18+，但本项目目标是 Node 20+）
- 支持 `utf8mb4` 的 MySQL 8（已提供 Docker Compose）
- 一个独立的 Kali Linux VM / 服务，用于真实环境中的 Runner

## 项目启动方法

```bash
copy backend\.env.example backend\.env
copy kali-runner\.env.example kali-runner\.env
copy codex-bridge\.env.example codex-bridge\.env
copy frontend\.env.example frontend\.env
docker compose up -d mysql
cd backend && pip install -e ".[dev]" && alembic upgrade head
cd ..\kali-runner && pip install -e ".[dev]"
cd ..\frontend && npm install
cd ..\codex-bridge && npm install
```

分别在不同终端启动各个服务：

```bash
cd backend && uvicorn app.main:app --reload --port 8000
cd kali-runner && uvicorn app.main:app --port 8091
cd codex-bridge && set CODEX_MOCK_MODE=true && npm run dev
cd frontend && npm run dev
```

打开 Vite 地址（通常是 `http://localhost:5173`）。前端只调用 FastAPI。

## 迁移流程

```bash
cd backend
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

`APP_DATABASE_URL` 默认使用 `mysql+asyncmy`。本地测试时它可以指向异步 SQLite URL；生产环境仍然使用 MySQL 8。

## 单智能体 OpenAI 兼容解题循环

创建任务时选择 **OpenAI Compatible**，并选择一个已启用的模型配置。后端会基于题目信息、观测、工具摘要和工件元数据构建受限上下文；随后要求模型返回严格的 `AgentAction` JSON，使用 Pydantic 验证后才调用现有 Tool Gateway。完整工具输出会保留在工作区工件中，需要时可通过 `file_read` 读取。

该循环会对智能体步数、工具调用次数、上下文观测数量和运行时长做持久化限制。它会发出持久化的 `agent.action_requested`、`agent.action_rejected` 和 `agent.action_completed` 事件。flag 候选项只会按题目 regex 验证，不会连接任何公开比赛平台。

请在两个服务环境中设置相同且非空的 `APP_RUNNER_API_TOKEN` 和 `RUNNER_API_TOKEN`。模型 API Key 会使用 Fernet 加密后落盘，且不会返回给浏览器。

可用的 PowerShell 辅助脚本：

```powershell
.\scripts\setup.ps1
.\scripts\start-backend.ps1
.\scripts\start-frontend.ps1
.\scripts\start-codex-bridge.ps1
.\scripts\test.ps1
```

## Mock 模式

- `engine_type=mock` 会发出无害的分析 / 计划 / 报告事件序列，并以未解出状态完成。
- `CODEX_MOCK_MODE=true` 会提供本地线程 ID 和结构化 mock 事件，而不依赖真实 Codex 运行时。
- Runner 仍然是真实且受限的；不存在通用命令执行接口。

## 已实现内容

- 题目 CRUD，包含 `target_url` 主机与 `allowed_hosts` 校验。
- 创建解题任务时生成唯一工作区、`challenge.json`、运行时 `AGENTS.md`，并记录持久化的 `run.created` 事件。
- 显式状态机；控制器不能分配任意状态。
- 持久化顺序事件，以及支持 SSE 回放 / 实时转发 / 心跳。
- MySQL / Alembic 架构、OpenAI-compatible 引擎骨架、Codex SDK 桥接、YAML 加载和工具审计模型。
- 工作区隔离、路径穿越检查、目标白名单、子进程参数数组、超时与输出上限。

## 明确未实现内容

自动 SQL 注入、命令执行、文件上传、漏洞利用 payload 库、宽泛扫描、多智能体自治、RAG、Docker 沙箱、认证、WebSocket、`codex app-server`，以及分布式队列。这些仍然是未来的设计项，不是隐藏能力。

## 验证

```bash
cd backend && ruff check . && pytest
cd kali-runner && ruff check . && pytest
cd frontend && npm run build
cd codex-bridge && npm run build
```

更多细节请查看 `docs/architecture.md`、`docs/api.md`、`docs/database.md`、`docs/deployment.md` 和 `docs/reference-analysis.md`。
