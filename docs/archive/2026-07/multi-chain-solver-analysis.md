# OpenAI Compatible 多链路解题基线分析

日期：2026-07-15  
代码基线：`67c09b1c30e0588c0a4405fc3d6158e1b0b2c9b1`

## 数据来源与边界

任务指定的 `2026-07-15-11-23.sql` 未出现在仓库、`D:\desktop` 或本次附件目录中，因此本报告没有伪造“已解析 SQL 文件”的结论。以下统计来自当前项目 MySQL 的只读查询，并以 `solve_runs`、`run_attempts`、`run_events`、`tool_calls` 和 `solver_states` 相互校验。

Runner 固定为 `http://192.168.236.128:8091`。本次修复不修改 Runner 地址，也不把远程 Runner 替换为本地 Runner。

## 总体基线

当前数据库共有 66 个 Run、158 个 Attempt：

| 引擎 | 状态 | 数量 |
| --- | --- | ---: |
| Codex SDK | COMPLETED_SOLVED | 11 |
| Codex SDK | FAILED_ENGINE | 15 |
| Codex SDK | FAILED_RUNNER | 1 |
| Codex SDK | PAUSED_RECOVERY | 3 |
| Codex SDK | CANCELLED | 6 |
| OpenAI Compatible | COMPLETED_SOLVED | 7 |
| OpenAI Compatible | COMPLETED_UNSOLVED | 11 |
| OpenAI Compatible | FAILED_ENGINE | 11 |
| OpenAI Compatible | TIMEOUT | 1 |

11 个 `COMPLETED_UNSOLVED` 中有 5 个最终 Attempt 完全没有逻辑工具调用。28 个 Run 出现过 `agent.no_progress`。

Easy JWT（challenge `fe6edaab-6e8d-4a34-a587-e7b269a38e7f`，目标 `http://192.168.236.1:18731`）的历史结果为：Codex SDK 4 次解出；OpenAI Compatible 6 次未解、1 次引擎失败。差异集中在控制面和工具编排，并非目标不可达。

## 指定 Run 对照

| Run | 当前显示计数 | Attempt 累计 | 最终状态 | 关键异常 |
| --- | ---: | ---: | --- | --- |
| `01bc3ca0-92cd-4ebc-a213-d0a3b3f4241f` | 1 step / 0 tool | 29 / 20 | COMPLETED_UNSOLVED | 5 个 Attempt；重启后累计计数丢失；重复读取推高 no-progress |
| `160bf58a-cd0b-46ee-b3b1-49a3b826d694` | 1 / 0 | 17 / 6 | COMPLETED_UNSOLVED | Skill 决策失败被当成攻击失败；后三次 Attempt 仅 2、2、1 step |
| `7aaec905-a37e-4eb4-aa5d-f494cb4cc188` | 1 / 0 | 19 / 13 | COMPLETED_UNSOLVED | 首次 Attempt 超时；第二次 1 step 即结束 |
| `93db4427-7495-4f2d-8079-a8d75b50d4eb` | 1 / 0 | 31 / 24 | COMPLETED_UNSOLVED | 解析错误、状态机错误和重复动作混入 no-progress；后两次 1 step 即结束 |
| `8adb9b60-dab5-4e85-83bd-694b5d7c65e4` | 20 / 15 | 20 / 15 | COMPLETED_UNSOLVED | no-progress=6 后直接报告，攻击链停在“已发现 JWT 线索” |

这组数据证明旧字段 `agent_step_count` / `tool_call_count` 同时承担“当前 Attempt”和“Run 累计”两个互斥语义。`restart()` 把它们清零后，FinishGate 和前端都误认为历史工作不存在。

## 根因

### 1. 提前结束是硬编码控制流

`orchestrator._stop_if_no_progress()` 在计数达到 6 时直接进入 `REPORTING -> COMPLETED_UNSOLVED`。事件中可以逐条看到 `agent.no_progress = 6` 后立即 `report.completed`。这违背了“6 次应放弃当前假设/切换链节点，12 次才在 checkpoint 请求用户”的恢复语义。

### 2. 控制面拒绝被错误计入漏洞负证据

以下事件都被累计为 no-progress：

- `SKILL_DECISION_REQUIRED`
- `SKILL_NOT_FOUND`
- `TOOL_INVALID_ARGUMENT`
- `TOOL_NOT_AVAILABLE`
- `DUPLICATE_ACTION`
- OpenAI Compatible JSON/schema 解析失败

这些只能说明动作没有执行，不能证明漏洞假设为阴性。它们还被写入 `rejected_paths_json`，使旧 FinishGate 误以为满足了“至少一个被排除路径”。

### 3. Skill 门禁阻断基础工具

多个 Run 在已经能够访问目标后，因 Specialist Skill 的 inspect/activate 失败而禁止 `http_request` 或 `file_search`。Skill 推荐本应是辅助信息，不应消耗 Agent Step，也不能成为基础工具的前置门禁。

### 4. 已确认能力没有驱动下一跳

Run 已经确认首页、`/debug.js`、登录/注册入口和 JWT 线索，但 `run_plan_json.next_actions`、`attack_surface`、`hypothesis_queue` 大多为空，Capability Ledger 通常只包含 `can_read_public_page`。因此“发现 JWT -> 获取会话 -> 解析 token -> 克隆 claims -> 签名 -> 注入 cookie -> 访问 admin”没有形成可执行依赖链。

### 5. HTTP/JWT 操作缺少安全的 Run 级句柄

现有工具能够发 HTTP 请求，却没有完整的 session、secret reference 和 JWT 变换操作。模型只能把 Cookie/JWT 明文搬运到后续参数中，既容易被上下文裁剪，也不符合密钥不落日志的要求。

### 6. FinishGate 只检查“有过一些工作”

旧门禁只要求基线来源、一个 rejected path、一个 hypothesis、无待审 flag。它不检查 Attempt/Run 步数、有效逻辑工具数、实验维度、真实 NEGATIVE/BLOCKED 结论或仍可执行的高优先级节点，所以 1 step / 0 tool 的重启 Attempt 仍可结束为未解。

## 修复方向

1. 拆分 Run Total、Attempt 和 Checkpoint Segment 计数，Restart 不再清空 Run Total。
2. 将执行结果分类为 `POSITIVE`、`NEGATIVE`、`BLOCKED`、`CONTROL_REJECTION`；只有前三类可参与攻击证据与 FinishGate。
3. no-progress 采用 2/4/6/8/12 分级恢复，6 次绝不结束任务。
4. 引入 AttackChainPlan 和 Capability Ledger reducer；能力确认后确定性解锁下一节点并重置 no-progress。
5. 完成 Run 级 HTTP Session、Secret Handle、JWT inspect/clone/sign/cookie-ref 工具，敏感值不进入事件、Prompt 或普通工具输出。
6. 将 PlanAction 与 ToolAction 分离；拒绝占位动作时返回可执行的规划要求，不把拒绝当漏洞阴性。
7. FinishGate 返回结构化 `missing_requirements`；同一 Attempt 两次提前结束后强制 PlanAction。
8. 前端同时显示当前 Attempt、Run 累计、距 checkpoint 和 Attempt 编号。

## 验收状态

本报告是修复前基线，不代表通过验收。只有迁移、后端/Runner/Bridge/前端验证以及题目双引擎三轮实测全部完成后，才能标记 `READY/PASSED`；缺少真实夹具或实际轮次时必须标记 `NOT_READY/NOT_PASSED`。
