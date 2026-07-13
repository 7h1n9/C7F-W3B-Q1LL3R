# Codex SDK 解题链路自检报告

生成时间：2026-07-14  
范围：`C7F-W3B-Q1LL3R` 当前本地数据库与代码基线  
安全说明：本报告不输出 API Key、Runner Token、Cookie、Session、数据库密码或真实 Flag。

## 结论

`CODEX_SDK_STATUS=NOT_READY`

原因是当前真实运行数据仍存在以下阻断项：

- 历史 Codex SDK Run 中存在大量直接工具事件：`command_execution=930`、`node_repl/node_repl.js=156`、`web_search=6`。
- Tool Gateway 工具事件为 `0`，说明真实 Codex SDK 解题链路尚未实际统一到 `Codex SDK -> ctfctl -> Backend Tool Gateway -> Kali Runner`。
- 存在终态 Run 仍残留 RUNNING Attempt 与活动 Lease：`0ae5494e-4fb1-4855-9d92-eb27ec024cf9`。
- 当前 Codex Bridge SDK 没有可靠取消线程能力，`cancel` 仍只能返回 `CANCEL_NOT_SUPPORTED`。
- Bridge 当前 `/health` 可达，但运行在 `mock_mode=true`，不能作为真实 Codex SDK 受控冒烟通过依据。

## 数据库统计

### Codex SDK Run 汇总

| 指标 | 数量 |
| --- | ---: |
| Codex SDK Run 总数 | 14 |
| COMPLETED_SOLVED | 8 |
| FAILED_ENGINE | 4 |
| CANCELLED | 1 |
| WAITING_USER | 1 |
| 长期非终态活跃状态 | 0 |
| Codex Thread ID 缺失 | 0 |
| Codex Thread ID 重复组 | 0 |

### Attempt / Lease

| 指标 | 数量 |
| --- | ---: |
| Attempt 总数 | 15 |
| 未关闭 RUNNING Attempt | 1 |
| 活动 Lease | 1 |

异常项：

- Run `0ae5494e-4fb1-4855-9d92-eb27ec024cf9` 当前为终态 `FAILED_ENGINE`，但 Attempt `e46b4300-9213-46ff-b0f7-d3f450be3472` 仍为 `RUNNING`，并残留 Lease `96d1e31a-e281-4551-9115-3c3c33fd1258`。

### 事件与工具边界

| 指标 | 数量 |
| --- | ---: |
| Codex 工具事件总数 | 1092 |
| `command_execution` 事件 | 930 |
| `node_repl/node_repl.js` 事件 | 156 |
| `web_search` 事件 | 6 |
| Tool Gateway / ctfctl 事件 | 0 |
| 事件序列缺失 Run 数 | 0 |

当前判断：历史数据证明 Codex SDK 仍在直接尝试平台工具，未真正落到 Tool Gateway。已在代码层加固：直接工具事件只作为策略违规审计，不再物化为 ToolCall、Artifact、Observation 或 Flag Candidate。

### Artifact / Observation / Flag / Report

| 指标 | 数量 |
| --- | ---: |
| Artifact | 318 |
| Observation | 289 |
| Flag Candidate | 16 |
| Report Artifact | 16 |

说明：当前库没有独立 `reports` 表，报告以 `artifact_type='report'` 的 Artifact 形式保存。

### Bridge 错误代码分布

| 错误代码 | 数量 |
| --- | ---: |
| INTERRUPTED_RESTART | 7 |
| CODEX_STREAM_ERROR | 3 |
| DATABASE_ERROR | 1 |
| RUN_INVALID_STATE | 1 |
| CODEX_BRIDGE_UNAVAILABLE | 1 |

## 本轮修复

### 诊断模式

新增 `CODEX_DIAGNOSTIC_MODE=true` / `APP_CODEX_DIAGNOSTIC_MODE=true` 支持。诊断模式下 Backend 启动不会：

- 关闭 Attempt；
- 删除 Lease；
- 把非终态 Run 标记为 `FAILED_ENGINE`；
- 自动恢复历史任务。

### Backend 自检接口

新增：

```text
POST /api/v1/codex-bridge/self-test
```

该接口只做 Bridge 链路诊断：

1. 访问 Bridge `/health`；
2. 创建隔离临时 workspace；
3. 创建测试 Thread；
4. 发送固定非靶场消息；
5. 检查是否收到 `agent.message` 与完成信号。

错误码按链路分层返回：

- `BRIDGE_UNREACHABLE`
- `BRIDGE_TIMEOUT`
- `CODEX_AUTH_FAILED`
- `THREAD_CREATE_FAILED`
- `EVENT_STREAM_FAILED`
- `EVENT_PARSE_FAILED`
- `EVENT_SEQUENCE_INVALID`

### Bridge health

Bridge `/health` 增加：

- `codex_sdk_loaded`
- `version`
- `active_threads`
- `mock_mode`

### 事件物化加固

`backend/app/services/codex_materializer.py` 已拒绝把以下直接工具事件物化为解题证据：

- `command_execution`
- `node_repl`
- `node_repl.js`
- `web_search`
- `shell`
- `powershell`
- `cmd.exe`
- `bash`

这些事件仍保留在事件流中用于审计，但不会成为 ToolCall、Artifact、Observation、Flag Candidate 或报告依据。

### 自动续跑防失控

Codex SDK 自动续跑循环新增连续无进展判断。连续两轮快照无变化时转入 `WAITING_USER`，并写入原因：

```text
CODEX_NO_PROGRESS
```

快照维度：

- RunEvent 数量；
- Artifact 数量；
- Observation 数量；
- Run 状态。

## 当前未解决项

- 真实 Codex SDK 尚未通过 `ctfctl/Backend Tool Gateway` 工具链路完成受控冒烟。
- Bridge SDK 当前不支持可靠终止已运行 Thread。
- 历史数据库中存在直接工具调用事件，不能作为学习 Skill 或成功经验复用依据。
- 当前运行中的 Bridge 是 `mock_mode=true`，真实 SDK 加载和认证还需要在非 mock 模式下重新验证。
- 终态 Run 残留 RUNNING Attempt/Lease 需要在非诊断模式启动时由 Reconciler 回收，或在确认后执行一次受控修复。

## 验证记录

已执行：

```text
backend: python -m ruff check app tests
backend: python -m pytest tests -q
backend: alembic upgrade head -> downgrade -1 -> upgrade head（临时 SQLite 库，未触碰当前 MySQL 现场）
codex-bridge: npm run build
codex-bridge: npm test
frontend: npm run build
```

结果：

```text
backend ruff: passed
backend pytest: 50 passed
alembic cycle: passed
codex-bridge build: passed
codex-bridge test: passed
frontend build: passed
```

新增接口现场验证：

```text
CODEX_DIAGNOSTIC_MODE=true
临时 Backend: http://127.0.0.1:18002
POST /api/v1/codex-bridge/self-test
result: OK
bridge mock_mode: true
```

解释：self-test 证明 Backend -> Bridge -> Thread -> Event Stream -> Backend Event Adapter 的 mock 链路可用；由于 Bridge 当前是 `mock_mode=true`，不能据此判定真实 Codex SDK 链路 READY。

## 页面数据加载性能修复

用户反馈项目显示数据慢后，定位到慢点主要是：

```text
GET /api/v1/runs
```

修复前现场测量：

```text
/api/v1/runs: 约 1575ms - 1688ms
/api/v1/diagnostics/runs: 约 487ms - 561ms
/api/v1/challenges: 约 23ms - 25ms
```

根因：

- 任务列表接口对每个 Run 都执行 `ensure_codex_materialized()`。
- `read_with_summary()` 默认对每个 Run 执行深度诊断 `run_diagnostics_service.analyze()`。
- Codex SDK 历史事件量较大时，普通列表页会反复扫描事件、工具调用、Observation、Artifact 和 Flag Candidate。

修复：

- `GET /api/v1/runs` 改为轻量列表路径。
- 列表页不再逐个 Run 执行 Codex 物化。
- 列表页不再逐个 Run 执行深度诊断。
- 详情页、工作区、诊断页仍按需执行物化和深度诊断。
- 增加回归测试，确保 Codex Run 列表不会再次触发物化和深度诊断。

修复后在临时 Backend 上现场测量：

```text
/api/v1/runs: 约 190ms - 245ms
```

验证：

```text
backend ruff: passed
backend pytest: 51 passed
```

## Codex mock 回显自动续跑修复

用户提供的任务时间线显示连续出现：

```text
[mock] Authorized workspace ... Resume the authorized analysis.
Codex 分析回合已完成，准备自动继续
```

确认原因：

- 运行中的 Codex Bridge `/health` 返回 `mock_mode=true`。
- `scripts/start-all.ps1` 之前强制设置 `CODEX_MOCK_MODE=true`，导致按脚本启动时永远使用 mock Bridge。
- 后端自动续跑的无进展判断把新增 `agent.message` / 回合事件也算作进展；mock 每轮都会生成新的 `item_id`，因此不会触发 `CODEX_NO_PROGRESS`。

修复：

- `scripts/start-all.ps1` 默认不再启用 mock Codex；如需 mock，必须显式传 `-UseMockCodex`。
- `scripts/start-codex-bridge.ps1` 默认设置 `CODEX_MOCK_MODE=false`；如需 mock，必须显式传 `-UseMockCodex`。
- 后端 Codex 自动续跑的进展快照不再统计原始 RunEvent 数量，只统计：
  - ToolCall；
  - Artifact；
  - Observation；
  - FlagCandidate；
  - Run 状态。
- 纯 agent 回显、`turn.completed`、`agent.message` 不再算有效进展，因此 mock 回显或空转会在连续两轮后进入 `WAITING_USER`。

验证：

```text
backend ruff: passed
backend focused pytest: 6 passed
PowerShell startup scripts parse: passed
Bridge health after restart: mock_mode=false
Backend /api/v1/codex-bridge/self-test: OK
```

## 建议的下一步

1. 保持 `CODEX_DIAGNOSTIC_MODE=true`，先调用 `POST /api/v1/codex-bridge/self-test` 查看 Bridge 链路。
2. 关闭 Bridge mock mode，重新运行 Bridge 自检。
3. 配置 Codex SDK 的 MCP 工具暴露，使模型只能看到 `ctfctl/Backend Tool Gateway`，而不是直接平台工具。
4. 在确认 Reconciler 行为后回收残留 Lease/Attempt。
5. 使用最小本地靶题执行完整受控冒烟，只有通过后才能改为 `READY_FOR_CONTROLLED_SMOKE_TEST`。
