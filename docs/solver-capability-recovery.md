# CTF Agent 能力恢复报告

日期：2026-07-14

## 结论

当前实现已恢复工作区写入/同步、脚本执行、直接 ctfctl 工具、逻辑调用计数、可恢复工具错误、RunPlan/Capability Ledger 和重复读取缓存的主链路，但尚未满足最终 READY 条件。当前状态：

```text
SOLVER_CAPABILITY_STATUS=NOT_READY
```

原因是用户指定的 `2026-07-14-15-29.sql` 不在仓库或附件可读路径中，无法重新计算完整数据库统计；同时 Windows Runner 当前没有 OS 级网络命名空间/防火墙执行器，不能把 `script_run.network_mode=target_allowlist` 说成已完成隔离。七类题目各三次、两引擎完整基准也没有受控题目 fixtures 和运行环境，未作虚构。

## 1. 修改前统计与重点轨迹

当前输入中没有 SQL 快照，因此以下采用仓库已有的 `docs/solver-recovery-baseline.md` 脱敏基线，不把它冒充本轮 SQL 重算结果：

| 指标 | 已有基线 | 本轮 SQL 重算 |
| --- | ---: | --- |
| SolveRun 总数 | 37 | 不可用：SQL 缺失 |
| Codex SDK 成功率 | 8/19（42.1%） | 不可用 |
| OpenAI Compatible 成功率 | 5/18（27.8%） | 不可用 |
| FAILED_ENGINE | 17 | 不可用 |
| COMPLETED_UNSOLVED | 5 | 不可用 |
| Codex ToolCall | 393 | 不可用 |
| OpenAI ToolCall | 197 | 不可用 |
| Skill 激活失败 | 78 | 不可用 |
| Action 拒绝 | 35 | 不可用 |
| Bridge 超时/不可达 | 8 | 不可用 |
| 工作区同步失败线索 | 15 | 不可用 |

现有脱敏重点轨迹确认了：Bridge resume 超时被提升为引擎失败、直接命令工具拒绝被错误终止、ctfctl 不可用、空身份 Skill 循环、重复 file_read、以及 OpenAI 过早 unsolved。对应 fixture `solver_recovery/focus_runs.json` 已用于定位，不含密钥、Cookie、Token 或 Flag 明文。

仓库中 6 个 OpenAI 脱敏历史 fixture 仅用于回归结构：2 个已解出任务平均 9 个工具，4 个失败任务平均 21.5 个工具；这不是完整数据库的最近 15 个任务统计。

## 2. 工作区权限模型

`backend/app/services/workspace_policy.py` 新增 `WorkspacePolicy(run_id, attempt_id, lease_token, workspace_root)`。Backend ctfctl 每次请求同时校验 Run、Challenge、Workspace、活动 Attempt、Lease token、Attempt 状态和 Lease 过期时间；终态、取消、暂停检查点没有工具执行资格。

| 区域 | 读取 | 写入 | 删除 |
| --- | --- | --- | --- |
| `challenge.json`、`AGENTS.md` | 是 | 否 | 否 |
| `source/**`、`attachments/**` | 是 | 否 | 否 |
| `requests/**`、`responses/**`、`outputs/**`、`evidence/**`、`final/**` | 是 | 仅生成/报告路径 | 否 |
| `scripts/**`、`scratch/**`、`payloads/**`、`notes/**`、`generated/**`、`extracted/**` | 是 | 是 | 仅 agent 生成子树 |
| 其他 Run、项目仓库、`.env`、数据库、用户目录、浏览器/Git 凭据 | 否 | 否 | 否 |

`workspace_extract_archive` 只接受 `attachments/**` 或 `scratch/**`，输出固定到 `extracted/**`，检查绝对路径、`..`、符号链接、文件数和总解压大小。

## 3. 新增工作区工具与同步

新增 `workspace_list`、`workspace_tree`、`workspace_stat`、`workspace_read`、`workspace_search`、`workspace_write_file`、`workspace_patch_file`、`workspace_mkdir`、`workspace_copy`、`workspace_move_generated`、`workspace_delete_generated`、`workspace_extract_archive`。

`WorkspaceManifestService` 生成 `relative_path/size/sha256/modified_at/source/backend_present/runner_present`；`WorkspaceSyncService` 调用 Runner manifest 做增量上传，并对 `outputs/**`、`evidence/**`、`responses/**`、`final/**` 做受限反向同步。脚本运行前会先同步 Backend 文件并按 SHA-256 检查 Runner 状态。

## 4. script_run 与 sandbox_exec

Runner 新增：

- `script_run`：支持 `python`、`node`、`bash`，仅允许 `scripts/**` 和 `scratch/scripts/**`，argv 分离，不启用 shell，返回 exit code、stdout/stderr 摘要、生成文件 SHA-256、运行时间和网络目标元数据。
- `sandbox_exec`：仅允许 `file`、`strings`、`grep`、`sed`、`awk`、`jq`、`xxd`、`base64`、`openssl`、`unzip`、`tar`、`7z`、`binwalk`、`exiftool`，禁止 shell、管道、重定向、命令拼接和网络模式绕过。

当前实现已有超时、输出上限、argv 校验、路径边界和 Runner Capability 返回；但 Windows 环境尚未接入 OS 级网络隔离、内存/CPU/进程数/文件大小的完整继承限制，所以不能宣称脚本网络安全要求已经闭环。

## 5. ctfctl、逻辑调用与授权

Bridge 默认直接展示 `ctfctl.http_request`、`ctfctl.http_session_request`、`ctfctl.http_extract`、`ctfctl.content_discovery`、`ctfctl.python_run`、`ctfctl.script_run`、`ctfctl.jwt_inspect`、`ctfctl.file_type`、`ctfctl.strings_extract`、`ctfctl.sandbox_exec` 和工作区工具；旧 `invoke_tool` 仅保留为兼容端点，不再列入 MCP 工具清单。工具名只保留一层 `ctfctl` 命名空间，不再生成 `ctfctl.ctfctl.*`。

`ToolCall` 新增 `logical_tool_call_id`、`parent_tool_call_id`、`execution_layer`。Backend Gateway、Runner、Extractor 作为内部链路记录；预算和 Run 指标按逻辑调用计数。工具状态允许 `ANALYZING`、`PLANNING`、`EXECUTING`、`EVALUATING`、`RETRYING`，由当前 Attempt/Lease 边界保护，不再仅因瞬时 `PLANNING` 拒绝合法动作。

`TOOL_INVALID_ARGUMENT`、`FILE_NOT_FOUND`、`SCRIPT_NOT_SYNCED`、`SKILL_NOT_FOUND`、`RUN_TOOL_NOT_ALLOWED`、`TOOL_NOT_INSTALLED` 等错误现在反馈错误原因、可用工具、可读区域、推荐替代动作和可重试标记；不会仅因这类工具失败直接转成 `FAILED_ENGINE`。

## 6. 方法论、历史经验和上下文

新增/接入：

- Decision Card：已知事实、唯一核心问题、两种可能、成功信号、失败转向。
- RunPlan：目标、阶段、攻击面、已确认能力、开放问题、假设队列、当前实验、下一步和退出条件。
- Capability Ledger：记录 `can_read_file`、`can_authenticate`、`can_reuse_session`、`can_control_parameter`、`can_read_file`、`can_upload_file`、`can_trigger_template_render`、`can_inject_sql`、`can_forge_token`、`can_access_admin`、`can_extract_flag` 等已确认能力，避免重复验证。
- Experiment：每次动作保存问题、假设、信号、工具、参数形状、`POSITIVE/NEGATIVE/INCONCLUSIVE/BLOCKED/ERROR` 分类和下一决策。
- `ChallengeLessonService`：默认 `strategy_only`，只提取工具顺序、攻击面、阻断码、参数键形状和重复行为；不提取 Flag、Cookie、Token、密码、动态端口、Run ID 或模型隐藏推理。正式基准可设置 `APP_HISTORICAL_LESSON_MODE=disabled`。
- ContextBuilder：加入 RunPlan、能力账本、已读索引和最新实验，普通上下文硬预算降到 40000 字符，历史经验改为策略摘要。
- Skill：Specialist 激活要求结构化证据；激活失败会回到基础工具循环，不阻断解题。

OpenAI 结束门槛现在拒绝“0 Tool Call 直接 unsolved”，要求题目/基线、有效 Observation、两个方向或明确不可恢复 Blocker。`file_search` 返回匹配路径/摘要/行号；相同 `file_read` 范围命中 `FILE_RANGE_ALREADY_READ` 缓存，不再次调用 Runner。

## 7. UI 与验证

工作区页面新增当前决策卡、RunPlan 目标和 Capability Ledger 展示；原有状态、假设、排除路径、技能和工具审计继续保留。

本轮真实验证：

| 检查 | 结果 |
| --- | --- |
| Backend pytest | 55 passed |
| Runner pytest | 12 passed |
| Backend 新代码 Ruff | passed |
| Runner Ruff | passed |
| Bridge build/test | passed |
| Frontend build | passed |
| Alembic upgrade → downgrade -1 → upgrade | passed，临时 SQLite |
| Runner 脚本链路 | passed：上传 `scripts/test.py` → manifest → `script_run` → `outputs/generated/result.txt` |
| 七类题目两引擎各三次 | 未执行：缺少受控 fixtures/目标实例 |

## 8. 尚未解决的问题

1. `2026-07-14-15-29.sql` 缺失，无法完成用户要求的本轮数据库精确统计和修改前后真实平均 Logical Tool Call 对比。
2. Windows Runner 未接入 OS 级网络命名空间/防火墙或容器沙箱；当前 `target_allowlist` 不能作为已完成的强制网络边界。
3. CPU、内存、进程数和文件大小限制尚未做到跨解释器、子进程继承的完整 OS 级保证。
4. 没有可复现的七类题目目标实例，因此不能报告题目成功率、基础题 ≤10/中等题 ≤20 或重复读取率 <5% 的正式基准。

因此当前只能输出 `SOLVER_CAPABILITY_STATUS=NOT_READY`。
