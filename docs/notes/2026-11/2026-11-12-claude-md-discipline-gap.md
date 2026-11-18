# claude-cli Worker 拿不到执行纪律——CLAUDE.md 与 AGENTS.md 的断裂

日期：2026-11-12
触发案例：`94af75e5`（培正集团，三区渗透自建题）

## 一、先澄清：这道题本身不足以归因

调查中先发现环境状态不一致，必须记录在此以免误用：

| 观测 | 三个 Run 全部看到 | 我 8 小时后访问 |
|---|---|---|
| `/console` 的「已控主机」台数 | **0 台** | **4 台** |

`8799ee2f` / `579dfcd7` / `94af75e5` 的五个 worker 日志里，`DMZ-WEB` / `10.10.10.11` /
`OPS-ZBX` / `192.168.17.212` **出现 0 次**；而 `/console` 页面现在直接列出这 4 台。
另外 `/srv/oa/upload/r.php`（内容 `<?php print_r(__db('select flags from flag')); ?>`）
在任何 Run 的日志中均**未被提及**，即不是 agent 上传的。

结论：**靶机实例在 Run 期间与当前的状态不同**，因此「agent 没读页面」这类归因不成立。
我随后改用全库数据做通用分析，本文只记录有数据支撑的部分。

## 二、被否定的三个假设

用 325 个 Run（命令数 ≥30）、36942 条命令验证：

### 假设 A「重复请求 = 卡死」——否定

按「完全相同的命令」占比重分组：

| 分组 | Run 数 | 解出率 |
|---|---|---|
| 完全重复 >30% | 91 | **35%** |
| 完全重复 ≤30% | 234 | 29% |

重复更多的组反而更高。盲注、爆破、参数迭代天然重复，不能当缺陷。

### 假设 B「端点多样性别低 = 无效探测」——否定

端点多样性中位仅 0.21（79% 请求重复），218 个 Run <0.30。但结合 A 的结论，
这反映的是任务性质，不是行为缺陷。

### 假设 C「claude worker 被停滞检测器误杀，跑道更短」——否定

| 主引擎 | Run 数 | 命令中位 | 解出率 |
|---|---|---|---|
| claude | 186 | 82 | 30% |
| codex | 181 | 78 | **37%** |

跑道基本一致（82 vs 78）。解出率有 7 个百分点差距，但**不可据此断言因果**
（模型、协议、样本都不同）。

### 附：fact 产出率的正确读法

「真实 fact 产出率 <1/100cmd → 解出率 3%，≥3/100cmd → 44%」看起来是强指标，但：

1. `fact_added` 事件是**结束时批量落库**（三个 Run 的 fact 全部出现在命令序列
   89%~100% 位置），不能当作过程中的实时行为。
2. `_stagnation_limit_for_progress()` 的设计是「**每产出一条 fact，停滞预算增加
   `stagnation_progress_bonus_per_fact`**」。所以是「fact 买跑道」，而非「fact 导致成功」。

该指标可用，但含义是「结束时能提取出多少已确认事实」。

## 三、真正的缺陷：CLAUDE.md 从未包含执行纪律

### 证据链

代码侧（`muteki_runtime.py::_seed_known_exploit_block`，注释原文）：

> "its ``AGENTS.md`` is the only file both CLI Workers (Codex, Claude Code) read
> on every turn"

这个断言是错的。Claude Code 读**当前工作目录的 `CLAUDE.md`**，AGENTS.md 是 Codex 的约定。

实测 run `94af75e5` 的 `muteki/` 目录：

| 文件 | 长度 | 含 `VERIFIED_FACT` 指令 |
|---|---|---|
| `AGENTS.md`（后端写） | 4005 | **1** |
| `CLAUDE.md`（镜像 seeding） | 1825 | **0** |

而该 Run 的主力 worker 正是 claude-cli（日志 454 KB，codex 仅 20 KB）。

### 影响量化

同一 Run 内两个 worker 的标记产出：

| Run | claude-cli | codex-cli |
|---|---|---|
| `1a670d18` | **0** | **14** |
| `c2991212` | 2 | 0 / 1 |
| `78889b76` | 1 / 2 | — |

codex（读 AGENTS.md，有指令）明显更多。claude 偶有 1~2 次，属模型自主，不稳定。

代码里本来就有针对性修复——`known_exploit.py` 的 `EXECUTION_CHECKLIST` 含：

> "### Report findings WHILE you work, not when you finish"
> "Print **`VERIFIED_FACT=<finding>` the moment a command proves it**"

注释记录了 2026-10-25 `4ba94181` 的同类案例（首个 fact 落在 t+1520s）。**但这段只写进了
AGENTS.md，claude worker 永远看不到。**

### 时序与覆盖的关键验证

`CLAUDE.md` 由镜像内 `runtime-agent` 在容器启动时 seeding。若它会覆盖，后端预写就无效。

**实测**：把一个预写 `CLAUDE.md`/`AGENTS.md` 的目录挂载进 worker 镜像启动，
两个 marker 均存活 → **runtime-agent 不覆盖已存在文件**。

因此后端可以安全预写 `CLAUDE.md`。

## 四、改动

### 4.1 `app/services/workspace.py`

新增 `CLAUDE_ENV_PREAMBLE`（455 字符的环境摘要：工具可用性、离线知识库路径）
与 `_sync_claude_md(workspace, agents_md)`（公开别名 `sync_claude_md`）。

- 写入 `CLAUDE.md` = 环境摘要 + AGENTS.md 全部内容
- 幂等：已含 `CLAUDE_SYNC_MARKER` 时只替换正文，不叠加前言
- `OSError` 不抛出：写入失败不能阻断 workspace 创建

环境摘要必须内置，因为镜像里的那份在后端不可达，覆盖后会丢失。

### 4.2 `app/solver/muteki/runtime/muteki_runtime.py`

新增 `_sync_worker_claude_md(root)`，在 `_seed_known_exploit_block(root)` **之后**调用
（先写 AGENTS.md，再镜像到 CLAUDE.md），读 `muteki/AGENTS.md` 交给 `sync_claude_md`。

**这一步是「生效」的关键**：muteki 的 Sandbox 只挂载 `<workspace>/muteki`，
而 `create_workspace` 写的是根目录 AGENTS.md。只在 `create_workspace` 里加同步会写错位置——
典型的「产出≠生效」。

### 4.3 测试

`backend/tests/test_claude_md_sync.py`，6 条：

- 正文写入
- 环境摘要保留
- 重复同步不叠加
- 镜像 seeding 的旧文件被替换而非追加
- 只读 workspace 不抛异常
- 前言长度上限（<900 字符，防止把纪律挤下去）

## 五、验证

用 `94af75e5` 的真实文件在副本上执行：

| 指标 | 修改前 | 修改后 |
|---|---|---|
| `CLAUDE.md` 长度 | 1825 | **4460** |
| 含 `VERIFIED_FACT` | 0 | **1** |
| 含环境说明（knowledges） | ✓ | ✓（保留） |
| 含执行纪律标记 | ✗ | ✓ |

回归：**2496 passed, 0 failed**（原 2490 + 新增 6）。

## 六、待观察 / 未做

1. **效果需真实 Run 验证**：`94af75e5` 这类题的后续 Run，claude worker 的
   `VERIFIED_FACT` 产出是否上升。这是唯一能证明生效的指标。
2. **不要期待解出率立刻跳升**：7 个百分点的引擎差距可能有多个成因，本次只消除了
   一个明确的信息断层。
3. `_seed_known_exploit_block` 的错误注释已在新方法 docstring 中说明，但原注释未改
   （避免动 vendored 上下文之外的措辞），后续可一并修正。
4. `/console` 的报错文案「该主机尚未拿到 shell 权限」对 `h` 参数格式错误有误导性，
   属题目/平台侧，未改动。
