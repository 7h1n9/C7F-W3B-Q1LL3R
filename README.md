# C7F-W3B-Q1LL3R

> 面向授权场景的多引擎 Web 自动化解题框架（Muteki Solver Runtime）

C7F-W3B-Q1LL3R 是一个基于 [Muteki（無敵）](https://github.com/FishCodeTech/muteki) 方法论重构的 Web 自动解题工作流单仓库。它通过「黑板（SharedGraph）+ 协调器（Coordinator）+ 多 Worker 并行」的方式，对授权的 CTF 靶场 / 测试目标完成侦察、分类、规划、执行、证据收集与完成判定。

本项目仅用于本地练习靶场、CTF 比赛以及明确授权的测试场景。它不会提供任意 shell 命令执行、公共目标自动化、持久化、自动化利用、宽泛扫描或自动化 payload 库。

## 核心特性

- **Muteki 解题链路**：完整迁移官方 Muteki 的 Prepare -> Race -> Coordinator -> Finalize 阶段，包含 Reason 规划、Worker 池、Review 复核、Strategy 策略与 Gate 完成判定。
- **多引擎并行**：支持 `codex_cli`、`openai_compatible` 两类 Worker 引擎；创建任务时可同时勾选多个引擎并行执行，Coordinator 的 Reason 模型可独立选择。
- **容器化执行**：Worker 默认运行在 Docker 容器（`muteki-worker`）中，通过反向连接控制面与宿主通信；Kali Runner 已降级为可选遗留服务，不再由启动脚本管理。
- **证据驱动完成**：Flag 候选必须通过格式校验、非占位符校验以及「必须出现在真实输出中」的证据校验；无证据不得判定完成。
- **持久化与恢复**：Run、检查点与事件全部落库；进程重启后自动恢复未完成任务，检测被中断的 Action 并生成恢复反馈，不静默重复执行。
- **可观测性**：全链路审计事件（run / action / completion）、SSE 实时推送、按模型的成本与 Token 用量统计。
- **前端工作区**：实时阶段 / Worker / Fact 展示、案情分析板（证据与事实语义化）、POC 导出、Writeup 生成、模型用量明细。

## 架构

```text
React + TypeScript 前端
        | REST / SSE
FastAPI 后端 ---- MySQL 8（:3307）
        |
RunSupervisor -> Muteki Runtime -> MutekiOrchestrator
        |
        +-- SharedGraph（黑板，状态唯一来源）
        +-- Reason（规划，可独立选择推理模型）
        +-- Worker 池（多引擎并行）
        +-- Review / Strategy / Titler
        +-- Gate（证据驱动完成判定）
        |
Worker 执行边界（默认 Docker 容器，反向连接 :9100）
```

Muteki 阶段流转：

```text
PREPARE -> RACE -> COORDINATOR（循环）-> FINALIZE
```

- `PREPARE`：创建工作区、初始化 SharedGraph、探活引擎。
- `RACE`：侦察目标，写入 `ENDPOINTS_DISCOVERED`、`AUTH_REQUIRED`、`SESSION_COOKIE` 等 Fact，并生成 `CHALLENGE_CLASSIFICATION` 分类。
- `COORDINATOR`：Reason 依据黑板生成 Intent，Worker 认领执行，Observation 写回黑板，Review 复核，循环直到完成条件成立或停止。
- `FINALIZE`：通过 Gate 校验 Flag 与证据链，生成报告并结束 Run。

## 目录结构

| 目录 | 说明 |
| --- | --- |
| `backend/app/` | FastAPI 应用：API、服务、引擎、Solver Runtime |
| `backend/app/solver/muteki/` | 生产 Solver Runtime（Coordinator、Reason、Worker、Strategy、Gate、Titler、事件） |
| `backend/muteki/` | 官方 Muteki 核心（swarm / solver / sandbox / learning / models） |
| `backend/app/services/` | Run 编排、事件、报告、案情分析语义、POC / Writeup 生成 |
| `backend/app/engines/` | OpenAI-compatible 引擎 |
| `kali-runner/` | 遗留受限工具服务（可选，不随 `start-all` 启动） |
| `frontend/` | Vite + React + TypeScript + Ant Design |
| `docker/`、`Dockerfile.worker` | Worker 容器镜像定义 |
| `ranges/` | 本地靶场实现（如 `asset-warranty-mysql`） |
| `data/` | 工作区、题目数据、日志、PID、基准目标 |
| `docs/` | 架构、API、数据库、部署等文档 |
| `scripts/` | 启动、停止、状态、测试脚本 |

## 环境要求

- Python 3.11+
- Node.js 20+
- MySQL 8（支持 `utf8mb4`，仓库已提供 Docker Compose，宿主机端口 `3307`）
- Docker（Worker 容器执行）
- 引擎凭据（按需）：Codex CLI 登录态 / API Key、OpenAI 兼容 API Key

## 快速开始

### 1. 准备环境变量

```powershell
Copy-Item backend\.env.example backend\.env
```

### 2. 一键启动（推荐）

```powershell
.\scripts\start-all.ps1                 # 后台启动 MySQL + 后端 + 前端
.\scripts\status-all.ps1                # 查看各服务状态
.\scripts\stop-all.ps1                  # 停止全部服务
.\scripts\restart-all.ps1               # 重启全部服务
```

启动完成后：

- 前端：`http://127.0.0.1:5173`
- 后端健康检查：`http://127.0.0.1:8000/api/v1/health/ready`
- 日志目录：`logs/services/`

### 3. 手动启动

```powershell
docker compose up -d mysql

cd backend
pip install -e .[dev]
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000

cd ..\codex-bridge
npm install
npm run dev

cd ..\frontend
npm install
npm run dev
```

### 4. 数据库迁移

```powershell
cd backend
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

`APP_DATABASE_URL` 默认使用 `mysql+asyncmy`，开发、测试与生产环境均要求 MySQL 8。

## 引擎与模型配置

| 引擎类型 | 说明 | 配置位置 |
| --- | --- | --- |
| `codex_cli` | 调用本机 Codex CLI，支持模型、推理强度与自定义 Responses API 端点 | 设置页添加（仅 Worker） |
| `openai_compatible` | 任意 OpenAI 兼容接口 | 设置页添加 `base_url` / `model_name` / API Key |

模型配置字段（设置页）：

- `provider_type`：`openai_compatible` 或 `codex_cli`
- `model_name`：模型名称；`reasoning_effort`：推理强度（`none` / `low` / `medium` / `high` / `xhigh` / `max` / `ultra`）
- `roles`：`worker`（解题 Worker）或 `coordinator_reason`（Coordinator 推理模型）；`codex_cli` 仅可作为 Worker
- 结构化输出、超时、重试、限流与并发上限等高级选项

创建任务时：选择目标题目 -> 勾选一个或多个 Worker 引擎 -> 选择 Reason 模型 -> 启动 Run。

## 前端页面

| 页面 | 说明 |
| --- | --- |
| 总览 `/` | 运行统计与入口 |
| 题目 `/challenges` | 题目 CRUD 与目标校验 |
| 任务 `/runs` | Run 列表、创建与状态管理 |
| 工作区 `/runs/:id` | 实时阶段 / Worker / Fact 展示、案情分析板、POC 导出、Writeup、用量明细 |
| 设置 `/settings` | 模型配置、引擎管理与系统设置 |
| 技能 `/skills` | 从历史 Run 沉淀的技能候选 |

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `APP_DATABASE_URL` | MySQL 连接串（默认 `localhost:3307`） |
| `APP_WORKSPACE_ROOT` | 工作区根目录 |
| `APP_CORS_ORIGINS` | 允许的前端来源 |
| `APP_ENCRYPTION_KEY` | API Key 加密密钥（Fernet） |
| `APP_ALLOWED_SERVICE_CIDRS` | 允许访问的服务网段 |
| `MUTEKI_CONTROL_BIND` / `MUTEKI_CONTROL_PORT` | Worker 反向连接控制面（默认 `0.0.0.0:9100`） |
| `MUTEKI_WORKER_IMAGE` | Worker 容器镜像（默认 `muteki-worker:codex-0.147.0`） |
| `APP_MUTEKI_WORKER_BACKEND` | Worker 执行边界（生产默认容器 `upstream_container`） |
| `VITE_API_BASE_URL` | 前端 API 地址（默认 `http://127.0.0.1:8000/api/v1`） |

## 测试

一键测试（后端 ruff + pytest、前端构建、桥接构建）：

```powershell
.\scripts\test.ps1
```

分模块验证：

```bash
cd backend && ruff check . && pytest
cd frontend && npm run build
```

`kali-runner` 为遗留服务，其测试可单独执行，不参与一键启动与一键测试的强制流程。

## 当前项目缺陷（已知限制）

> 以下内容反映 2026-08 实测状态（代码审计 + 真实运行记录 + `docs/phase2.4/` 阶段报告），会随版本迭代更新。

### 1. 解题成功率与题型泛化不足

- 当前稳定可解出的题目有限（如「历史文档预览中心」）；「企业采购订单查询系统」「企业内部项目审批门户」「企业邮件模板预览系统」「企业合同审核平台」等新题仍未稳定解出。
- 2026-08-13 三组 A/B 验证（单 Codex / Codex+OpenAI / Codex+双 OpenAI，目标：资产保修核验平台）全部超时（约 946s），且每组仅 1 次样本，尚不构成统计结论。
- 早期 Solver v2 真实运行样本为 13 次 11 解（84.6%），但该结论绑定特定题目与旧 Runner 路径，不能代表当前 Muteki 容器链路的泛化能力。

### 2. 多引擎并行调度不完善

- 勾选多个引擎时并非总能并行：曾出现「勾选 codex + openai，实际只有一路在跑」。
- OpenAI-compatible Worker 曾因只读兼容 `snapshot()` 而看不到兄弟 Worker 写入的持久化 Fact（已修复为读取 `verified_evidence()`），但死路 / 重复探索识别仍偏弱（曾出现已验证 144 条却无一条死路）。
- Codex CLI Worker 曾长期不可用（只有 `codex_sdk` 有效），模型与推理强度选择不生效的问题反复出现。

### 3. 运行时稳定性与资源管理

- 单 Run 可运行约 15 分钟直至超时，缺少基于进展的提前终止与失败识别。
- 高并发会耗尽目标 / 执行器响应能力（8 路并发实测 6 解 2 失败）。
- Run 结束后缺少容器自动回收机制，历史上产生大量残留容器与工作区。
- 数据库端口（3306/3307）、Runner 与容器执行边界在多轮迭代中多次混淆，说明配置入口仍不够集中。

### 4. 前端与披露质量

- Token / 成本用量曾长期显示为 0 或 N/A，按模型展开明细不稳定。
- 案情分析板早期只展示原文标签、证据密度不足，需要额外 AI 语义解析才能支撑人工理解。
- POC 导出与 Writeup 生成曾出现损坏 / 不可用，证据链信息不足以让用户独立复现解题过程。
- 前端目前没有任何自动化测试（0 个 test 文件），工作区功能回归风险高。

### 5. 测试体系覆盖面

- 后端有 112 个测试文件（历史全量 362 passed），但真实端到端（真实引擎 + 真实靶场 + 容器执行）用例少，多数为单元 / 契约测试。
- 容器链路依赖专有引擎 CLI 与凭据，未完全打通前健康检查 fail-closed，导致「容器能跑但引擎不可用」的中间状态。

### 6. 代码卫生与部署

- `kali-runner`、旧 Orchestrator、旧 Solver 等遗留代码仍保留在仓库中，增加维护与误用成本。
- 一键启动依赖本机 Docker、MySQL 与 Node/Python 环境，尚未达到「一条命令在任何机器可复现」的程度。


### 7. 与官方 Muteki 的架构差距（Web 题目领域）

> 以下分析基于 2026-08-18 对官方 Muteki 仓库（[FishCodeTech/muteki](https://github.com/FishCodeTech/muteki)）的代码审计，
> 聚焦 Web 题目解题链路相关模块。

#### 已具备的核心能力

- SQLite 事件溯源 SharedGraph（`graph.py`） ✅
- Coordinator 四阶段循环（Prepare → Race → Coordinator → Finalize） ✅
- Reason 分类规划器 + 工具域锁定（SQLI / IDOR / PATH_TRAVERSAL 等） ✅
- 多 Worker 池（Codex CLI、OpenAI Compatible、Claude） ✅
- Review Worker 复核机制 ✅
- Gate 完成判定（Evidence 驱动） ✅
- 容器化执行（`container_exec.py` + `control.py`） ✅
- 前端工作区、案情分析板、POC 导出、WP 生成 ✅

#### P0 — 最高优先级缺失

**1. `muteki-blackboard` Skill（Worker 原生黑板读写通道）** ✅ 已修复

官方 Muteki 每个 Worker 容器内预装 `muteki-blackboard` skill，Worker CLI 通过它直接读写黑板（事实、意图、死路），这是 Worker ↔ 黑板的唯一数据通道。当前项目已实现此标准化接口：
- `backend/app/solver/muteki/skill/blackboard.py` — 依赖零的 `sqlite3` CLI，支持 `read-facts`、`write-fact`、`mark-deadend`、`list-intents`、`claim` 等操作
- `backend/app/solver/muteki/skill/SKILL.md` — 面向 Worker 的 skill 定义与使用指引
- `backend/app/solver/muteki/cli_driver.py` — `run_skill` 方法供 Worker 调用
- Worker 提示词模板中已包含黑板技能的使用引导

**2. Insight Bus（Worker 间实时通信）** ✅ 已修复

官方 Muteki 的 `swarm/insight_bus.py` 实现 Worker 之间通过事件总线共享已验证事实和死路，实现“异构盲区不重叠”。当前项目已实现此机制：
- `backend/app/solver/muteki/insight_bus.py` — 基于 `asyncio.Queue` 的发布/订阅总线，支持 `FACT`、`DEAD_END`、`FLAG`、`GUIDANCE` 等消息类型
- `backend/app/solver/muteki/runtime/muteki_runtime.py` — 在 `_build_graph` 中创建 `InsightBus`，Worker 通过 `fact()`、`dead_end()`、`flag_found()` 发布，订阅/取消订阅由 Coordinator 管理
- 每个 Worker 拥有独立 inbox，发布者不接收自己的消息，新订阅者自动收到历史回放

#### P1 — 高优先级缺失

**3. 内容寻址存储（CAS）工作区**

官方 Muteki 使用 CAS（Content-Addressable Storage）管理工作区：

- `inputs/objects/` 按 sha256 分桶存储，相同文件只存一份
- `shared/objects/` 共享产物 CAS
- `workers/` 每个 Worker 的 scratch 目录使用相对符号链接指向 inputs/shared

当前项目工作区结构简单，没有 CAS 去重和符号链接隔离。多 Worker 并行时文件冲突风险，大文件重复存储。

**4. Hypothesis 假设驱动机制** ✅ 已修复

官方 Muteki 的 `SolveGraph` 提供 `add_hypothesis/set_status/active_hypotheses/mark_dead_end` 接口，支持“假设→验证→接受/否决”的显式流程。当前项目已通过 `graph.split_branch` / `resolve_branch` 和 `branches` 表实现假设生命周期管理，每个假设有 `open/accepted/refuted` 状态，由 Worker 或 Coordinator 驱动。

**5. Context 压缩 / 黑板摘要** ✅ 已修复

官方 Muteki 的 `solver/summarizer.py` 在黑板膨胀时压缩旧事实，控制 Worker 上下文窗口。当前项目已实现此模块：`backend/app/solver/muteki/summarizer.py` 提供 `compress_facts`、`compress_dead_ends`、`compress_intents`、`build_compressed_context`，保留已验证事实优先，候选事实次之，超出限制的聚合为总数摘要。

**6. Worker 路线（Lane）分配与锁定** ✅ 已修复

官方 Muteki 的 `shared_graph.py` 中 `lane_lock`、`route_hash`、`branch_id` 机制确保不同 Worker 走不同路线，不重复劳动。当前项目已实现完整路线分配与锁定机制：`coordinator.py` 的 `_RACE_LANES` 和 `_active_route_hashes`、`graph.py` 的 `suppress_route`/`reopen_route`/`routes`、`semantic_dispatch.py` 的 `try_claim_activity`/`release_activity`、`is_route_suppressed`。

#### P2 — 中等优先级缺失

**7. Worker Profile / 凭据管理体系**

官方 Muteki 的 `solver/worker_profiles.py` + `solver/credential_accounts.py` 提供结构化引擎配置、凭据注入、运行时环境管理。支持 `local` 和 `container` 两种模式，凭据通过 `_secrets/accounts/` 目录隔离。当前项目凭据管理分散，缺少统一的 Profile 注册机制。

**8. Cost Controller 精细化**

官方 Muteki 的 `core/cost.py` 精确追踪每个 Worker 的 token 消耗和成本，按模型统计。当前项目 `cost_bridge.py` 有基础实现，但前端显示和按模型明细还不够完善。

**9. Go 容器内 Supervisor**

官方 Muteki 的 `cmd/runtime-agent/` 使用 Go 编写容器内反向连接控制器，负责 Worker 生命周期管理、心跳、日志转发。当前项目使用 Python 实现（`control.py` + `container_exec.py`），稳定性和性能有差距。

**10. Learning / Distill 自学习** ✅ 已修复

官方 Muteki 的 `learning/distill.py` 支持从历史解题中提炼模板，实现自我进化。当前项目已集成此模块：
- `backend/muteki/learning/distill.py` — 上游核心已实现 `Template`、`TemplateStore`、`distill()`、`distill_from_events()`、`distill_and_store()`
- `backend/app/solver/muteki/runtime/muteki_runtime.py` — 在求解成功后自动调用 `_distill()`，从事件日志中提取证据链并保存为 YAML 模板
- `backend/app/services/retrieve.py` — 新增 `TemplateRetriever`，通过分类和关键词匹配召回相似模板，作为 Worker 提示词的 PRIOR 注入
- 知识库存储在 `data/knowledge/` 目录下，每个模板为独立的 YAML 文件

#### 建议优先修复顺序

1. **`muteki-blackboard` Skill** — 没有它 Worker 无法自主读写黑板，这是其他所有改进的基础设施
2. **Insight Bus** — Worker 间实时共享事实，直接提升多引擎并行效率
3. **CAS 工作区** — 多 Worker 并行时文件安全和空间效率的基础保障
4. **Hypothesis 机制** — Worker 从“盲目探索”升级为“假设驱动”的核心
5. **Context 压缩** — 防止长运行任务黑板膨胀，保持 Worker 提示词有效

### 8. 2026-09-08 真实 Run 复盘：链路阻塞修复状态

> 证据 Run：`c79dc3c8-d35c-4ada-9ece-3aa07d07c2de`（网络设备连通性诊断平台，TIMEOUT）、
> `35a8a67a-defd-4003-a3cf-be6112ea64fb`（企业通知模板中心，运行中/后续超时）。
> 两条 Run 的双引擎健康检查均通过、目标可达、HTTP 请求有真实响应。因此“解不出题”的主因
> 是解题策略/能力问题；以下链路问题会放大失败。前两项已于 2026-09-09 修复，后两项仍待处理。

- [x] **Worker 黑板技能 schema 不匹配（高优先级，已修复）**
  - 现象：容器内 `blackboard.py read-facts/read-deadends/read-review/read-directives/list-intents`
    统一报 `sqlite3.OperationalError: no such table: events`。
  - 证据：`c79dc3c8...` 与 `35a8a67a...` 的 Codex Worker 均复现；Worker 明确回退到 prompt 快照，
    并声明只能“以文本形式发布发现”，无法正常读写 SharedGraph。
  - 影响：多 Worker 无法通过官方黑板通道读取队友事实/死路，跨 Worker 协作退化为单轮提示词快照，
    容易重复探索或漏掉已确认路径。
  - 修复：`create_runtime_graph()` 在图库打开后统一切换为 SQLite `DELETE` journal 模式
    （避免 Docker Desktop bind mount 上的 WAL `disk I/O error`）；`_spawn()` 将
    `MUTEKI_BLACKBOARD_DB` / `MUTEKI_WORKSPACE` 改为容器内 POSIX 路径
    `/home/kali/workspace/...`，并显式设置 `MUTEKI_BLACKBOARD_SCRIPT=/usr/local/bin/blackboard.py`。
  - 验证：容器内 `blackboard.py read-facts` 已能成功读取 Run `35a8a67a...` 的事实/死路；
    `test_muteki_blackboard_runtime_fixes.py` 5 passed；相关回归 101 passed；compileall 与
    `git diff --check` 通过。

- [x] **超时预算与活跃 Intent 收口（高优先级，已修复）**
  - 现象：`c79dc3c8...` 在全局 30 分钟预算耗尽时仍有 2 个 open/claimed Intent
    （`web:login:auth:bypass`、`web:recon:api:docs`），最终以 `MUTEKI_RUN_TIMEOUT` 结束；
    `35a8a67a...` 的 `web:templates:ssti` Intent 被标记 `timed_out`，随后 Reason 出现
    `MODEL_TIMEOUT`。
  - 影响：Worker 已经产出 20/33 条事实和 9/3 条死路，但活跃方向在超时前没有收口，失败归因和
    后续复用都缺少最终结论。
  - 修复：`MutekiCoordinator.finalize()` 在第一个 `await` 之前同步执行
    `_close_active_intents()`，把 open/claimed Intent 统一收口为 `timed_out`（超时）或
    `cancelled`（正常结束），并写入 `finalize_active_intents` 指令事件与 `RUN_FINISHED` 统计；
    `MutekiRuntime.run_once()` 在全局超时时先 `request_stop("MUTEKI_RUN_TIMEOUT")`，再取消
    orchestrator task，确保 finalize 使用稳定的超时原因。
  - 验证：`test_muteki_blackboard_runtime_fixes.py` 5 passed；相关回归 101 passed；compileall
    与 `git diff --check` 通过。

- [ ] **原生 Worker 内部工具/证据投影不完整（中优先级）**
  - 现象：两条 Run 分别产生 20/33 条 Muteki fact、9/3 条 dead_end，但外圈只有 1 条
    `muteki_native_worker` ToolCall、1 条 Evidence、0 条 VerifiedFact。
  - 影响：内部 HTTP/Tool 调用未进入外圈 `ToolCall`/`EvidenceLedger`，导致证据链、PoC 导出、
    失败归因和论文实验指标不完整。
  - 修复方向：在原生 Worker 执行边界持久化结构化 RequestSpec/Observation，并与现有 Evidence
    authority 建立可追溯关联；不要把 prose 或 flag 结果反推成证据。

- [ ] **Codex 容器 shell 策略摩擦（低优先级）**
  - 现象：Codex Worker 的 `exec_command` 对包含 `rm -f` 的命令返回 `CreateProcess rejected`，
    模型需要改写命令后重试。
  - 影响：不阻塞核心链路，但会增加无效步骤和 token 消耗。

> 说明：上述 Run 的主要失败点仍是解题策略/能力，不是引擎、网络或工具不可用。
> `c79dc3c8...` 的 `/api/checks` 已返回 `command_preview`（如 `ping gateway.local`），但
> Worker 只测试了正常 `kind=ping&target=gateway.local` 和参数组合，没有提交 `;`、`|`、
> `$()`、反引号等命令注入载荷；`35a8a67a...` 的模板预览已返回 `audit_reference`，Worker
> 查询了自己的 `REF-2026-7405`（200），但没有系统枚举其他 `REF-2026-####` 记录做 IDOR
> 验证。这两点属于策略/能力问题，不能归因于链路不可用。

## 未来待开发功能

### 解题能力

- 补齐多题型策略：路径遍历、命令注入、SSRF、IDOR、JWT、SSTI、XXE、文件上传（`CHALLENGE_CLASSIFICATION` 已定义枚举，待对应工具域与策略完善）。
- 技能沉淀闭环：从历史 Run 自动沉淀可复用 payload / 策略 / 指纹，形成可检索的技能库。
- 多阶段、多目标任务编排与任务队列、并发上限控制。

### 多引擎调度

- 引擎健康检查失败后的自动重激活（不影响其他 Worker）。
- 基于路线（route hash）的公平调度与占用机制：同一路线只允许一个 Worker 执行，Reason 结果按路线归属。
- 更多引擎类型：浏览器 Agent、专用扫描器、本地命令行引擎。

### 运行时

- Run 容器生命周期管理：自动回收、超时回收、资源限制（CPU / 内存 / 网络）。
- 断点续跑与运行快照回放；跨进程 / 跨机恢复。
- 按 Run 设置 Token / 费用预算，超限自动降级或停止。

### 前端

- 案情分析板：标签自由拖放、批量 AI 语义解析、按证据链聚类。
- POC 一键导出可执行脚本；Writeup 面向人工复现的完整化（思路 + 步骤 + payload + 证据）。
- 用量明细按模型展开、实时成本预警。
- 前端单元测试与 E2E 测试。

### 平台化与安全

- 多用户、RBAC、任务隔离与审计导出。
- 目标白名单、工具白名单、命令沙箱等细粒度授权边界。
- 证据链防篡改（哈希链）与合规报表。

## 距离毕设水准的评估

### 已具备的毕设基础

- 完整解题链路：Challenge → RunSupervisor → Muteki Runtime（Prepare → Race → Coordinator → Finalize）→ Tool Gateway → Evidence → Completion Gate，并有真实靶场成功案例。
- 完整方法论支撑：基于 Muteki（無敵）的黑板 + 协调器 + 多 Worker + Review + Gate 架构，具备明确的研究问题与创新点。
- 文档齐全：架构 / API / 数据库 / 部署 / 阶段报告（`docs/`、`docs/phase2.4/`）。
- 后端测试 112 个文件，历史全量 362 passed；真实运行样本（13 次 11 解）可作为早期实验数据。

### 与毕设答辩要求的差距（按优先级）

| 差距 | 现状 | 达标建议 |
| --- | --- | --- |
| 多题型可复现成功率 | 仅个别题稳定解出，A/B 三组均超时 | 至少覆盖 3–5 类题型、每类 ≥3 题，成功率 ≥70% 且可复现 |
| 实验设计与对比 | 已有零散 A/B，样本不足 | 增加基线对比（旧系统 vs Muteki）、消融实验（单 / 多引擎、有无 Review / Gate）、失败案例分析 |
| 多引擎并行正确性 | 选多引擎不等于并行的问题未完全解决 | 修复调度后补充并行加速比、有效路线数、重复请求数等指标 |
| 可观测性数据 | Token / 成本统计不稳定 | 保证按模型用量、成本、耗时统计准确，作为论文性能指标 |
| 前端完整性 | 功能齐全但 0 自动化测试 | 补充关键流程测试（创建任务 → 运行 → 完成 → 导出） |
| 部署可复现性 | 依赖本机环境 | 提供一键 Docker Compose 全栈部署与一键验收脚本 |
| 代码卫生 | 遗留冗余代码仍在 | 清理 kali-runner、旧 Orchestrator 等死代码，收敛配置入口 |

### 结论

- 若毕设验收标准是「系统能演示完整链路 + 有架构与实验文档 + 真实靶场案例」：目前约完成 **70%**，主要缺口是实验数据的系统性与前端稳定性。
- 若标准是「多题型稳定可复现 + 论文级对比实验 + 代码整洁可维护」：预计还需 **1–2 个月**，重点是解题泛化、多引擎调度正确性、实验设计与代码清理。
- 建议把「Muteki 方法论迁移 + 证据驱动完成判定 + 多引擎并行调度」作为论文主线，这三块已有实现基础，且都有可量化指标支撑。
## 安全与限制

- 支持真实 CTF 比赛平台与授权靶场两种场景；Flag 候选只按题目 regex 校验，不连接任何公开比赛平台接口（无自动提交）。
- Flag Gate 要求：格式合法、非占位符、并且必须出现在真实输出中，防止「模型自认为完成」通过。
- 审计事件只保存身份、状态、原因与 `evidence_refs`，不记录原始响应、Cookie、Token 或密钥。
- 工具集由题目配置决定；系统不预置全局 payload 库、不自动对外部目标发动攻击；解题行为严格限制在目标题目网络边界内（`APP_ALLOWED_SERVICE_CIDRS`），不横向往无关地址扩散。
- 模型 API Key 使用 Fernet 加密后落盘，不会返回给浏览器。## 参考

- 架构与数据流：`docs/architecture.md`
- API：`docs/api.md`
- 数据库：`docs/database.md`
- 部署：`docs/deployment.md`
- Muteki 方法论与迁移：`docs/multi-agent-core.md`、`docs/phase2.4/`
- 上游项目：<https://github.com/FishCodeTech/muteki>
- 第三方声明：`THIRD_PARTY_NOTICES.md`、`backend/NOTICE_MUTeki.md`
