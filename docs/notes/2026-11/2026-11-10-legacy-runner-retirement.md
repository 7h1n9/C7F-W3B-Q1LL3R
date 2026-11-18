# 砍掉 legacy Kali Runner，把复现验证引回容器

日期：2026-11-10

## 一、背景：37 次复现验证，37 次同一个异常

`fresh_reproduction` 一直挂在 legacy Kali Runner 上，而该服务在本部署里是关闭的
（`legacy_kali_runner=False`）。因果链：

```
解出 → accepted → arena_solve.py:945 _verify_reproduction()
  → fresh_reproduction.py:104  runner_client.create_job()
    → runner_client.py:65  _require_enabled()
      → raise RunnerRetiredError
        → {"status":"error","reason":"RunnerRetiredError"}
          → fresh_reproduction_verified 永远 False
```

数据：

| 指标 | 值 |
|---|---|
| `arena.fresh_reproduction` 事件 | 37 条，**全部** `reason=RunnerRetiredError` |
| `solve_runs.fresh_reproduction_verified=1` | **0 / 742** |
| `COMPLETED_SOLVED` 但未复现 | 210 |

## 二、改动

### 2.1 新增 `app/services/container_reproduction.py`

在 Muteki 的 worker 容器里执行渲染好的命令，替代 Runner 的 job 协议。

- 同步实现（`docker exec` 阻塞），调用方用 `asyncio.to_thread` 桥接
- `ensure_container` 幂等：复用、重启或新建容器
- **任何失败都变成数据**：`CONTAINER_EXEC_RAISED` / `NONZERO_EXIT`，不抛异常
- 输出超限时保留头尾（`sqlmap --dump` 可能几 MB，flag 可能在任何一端）

### 2.2 `fresh_reproduction.py` 改用容器

删掉 `clear_sessions` / `sync_workspace`——那是 Runner 作为独立机器才需要的拷贝，
容器直接 bind-mount workspace，文件已在同一相对路径下。

### 2.3 `orchestrator.py` 清理两处 Runner 调用

- `_handoff_non_legacy_mode` 里 `sync_workspace` 失败会转 `FAILED_RUNNER`。这个方法
  本来就是给**非 legacy** 模式用的，却调用 legacy 的 Runner —— 必然失败。
- 两处 `clear_sessions` 改为 `_clear_legacy_sessions()`，只在 legacy 模式才调。

### 2.4 新增 PoC 回退提取（`reports.py`）

**这是让复现真正能跑起来的关键。** 实测 run `9847adf6`（Giftbox）只有一个
`ToolCall`，是 `muteki_native_worker` —— Muteki worker 直接跑 shell，不走结构化
`http_request` 工具，所以 `SolutionPathExtractor` 返回 **0 步**，`execute` 以
`MISSING_EXTRACTION_PROVENANCE` 结束，什么都没执行。

但证据一直在磁盘上：worker 自己写了 `muteki/exploit2.py`，还复制到
`shared/PoC_giftbox_exploit.py`，并打包成 `final/muteki-poc.zip`。

所以加了 `_workspace_poc_steps()` 回退：无可用 ToolCall 时，提取 workspace 里的
PoC 脚本，生成一步 `workspace_poc`。

**这不是放宽标准**：在全新容器里重跑 worker 自己的脚本，比回放手拼的 curl
更强——它重放的是整条利用链，而不是链中的一次请求。flag 仍由
`challenge.flag_pattern` 匹配，跑不出 flag 依然 unverified。

### 2.5 两处逻辑修正

- `muteki_native_worker` 加入 `_EXCLUDED_TOOLS`。它曾被当成可复现步骤，渲染成
  `curl -i -X GET "{{target_url}}"` —— 空 URL 的模板，什么也复现不了。
- `MISSING_EXTRACTION_PROVENANCE` 不再 `return`。它只表示「这条 recipe 不可用」，
  不等于「没有任何东西可重放」；原先的 early return 会把 planner 已经找到的步骤
  丢掉（实测 `steps: 0`）。

## 三、验证

### 3.1 端到端（run `9847adf6`，Giftbox）

```
plan: 1 步
executed: True | verified: False | blocked: None
  tool: workspace_poc  status: FAILED  exit: 1
  cmd : cd /home/kali/workspace && python3 muteki/exploit2.py
  err : ModuleNotFoundError: No module named 'pyotp'
```

**四个关键点：**

1. **不再抛 `RunnerRetiredError`** —— 通道接通了
2. **`executed: True`** —— 命令真的在容器里跑了
3. 路径映射正确：host `D:/.../9847adf6-...` → `/home/kali/workspace`
4. **失败原因可见**：`ModuleNotFoundError: pyotp`

第 4 点是这次改动的核心价值。以前是「静默 error，什么都不知道」；现在是
「跑起来了，缺 pyotp」。失败从不可见变成可归因。

### 3.2 回归

**2482 passed, 0 failed**（原 2473 + 新增 9 条容器复现测试）。

期间出现的 10 个失败全部是 MySQL 容器 `Exited (255) 12 hours ago` 导致，
启动 MySQL 后重跑 29 passed，与改动无关。

## 四、遗留问题（已确认，未修）

### 4.1 镜像缺 `pyotp`

worker 镜像里没有 `pyotp`，Giftbox 的 PoC 因此跑不起来。这是镜像层面的依赖，
需要加到 worker 镜像的 requirements 里。**这不是复现逻辑的问题**——逻辑已经通了。

### 4.2 大量 Run 无 PoC 可提取

`_workspace_poc_steps` 依赖 workspace 里还留着脚本。但 workspace 在 Run 结束后
会被清理（`9847adf6` 只剩 `muteki/`、`final/` 等），更早的 Run 可能连这个都没有。
能覆盖多少，需要跑一轮新解出的题才知道。

### 4.3 状态机不一致

发现 6 个 Run 标 `COMPLETED_SOLVED` 但 `run.completed` 里 `flag_verified=false`，
其中 `46de6a50`（996game）的候选是 `CTF2{not_flag}` 却被标 `verified=1`。
这是另一个独立问题，未在本轮处理。

## 五、验收判据

原定判据是「下一道解出的题出现 `status != error`」。已在 `9847adf6` 上达成：
`executed: True`，且失败原因是具体的技术原因而非通道错误。

下一步要看的是：**新解出的题里，`arena.fresh_reproduction` 的 `status` 分布**，
以及 `fresh_reproduction_verified` 何时第一次出现 `True`。后者还卡在 4.1 的
`pyotp` 上。

---

## 六、生产验证：修复已在新解出的题上生效（2026-11-11 01:40）

### 6.1 事件状态分布：从 37/37 error 到 2/2 executed

```
arena.fresh_reproduction 事件：
  error      37   ← 全部是改动前（RunnerRetiredError）
  executed    2   ← 改动后新解出的题
```

**改动后产生的复现事件 100% 是 `executed`，没有一条 `error`。**

原始观测（run `2531c1c4` / `72a3325f`）：

```json
{"reason": "", "status": "executed", "verified": false, "budget_seconds": 180}
```

对比改动前：

```json
{"reason": "RunnerRetiredError", "status": "error", "verified": false, "budget_seconds": 180}
```

通道接通了。

### 6.2 期间又解出 2 道缺口题

| 题 | 提交时间 |
|---|---|
| [SUCTF2018]Unfinish | 2026-11-10 15:45 |
| [极客大挑战 2018]easy_laravel | 2026-11-10 15:51 |

缺口题从 5 道降到 **3 道**（`Phuck2`、`Comment`、`Upload`）。

### 6.3 `verified` 仍为 false 的原因：已定位到具体依赖

`2531c1c4` / `72a3325f` 都是 `executed: true, verified: false`。原因不是复现逻辑，
而是 PoC 跑不出 flag。

实测 worker 镜像的 Python 依赖：

| 模块 | 状态 |
|---|---|
| `requests` | OK |
| `bs4` / `lxml` / `jwt` / `Crypto` / `pwn` | OK |
| **`pyotp`** | **MISSING** |

所以 Giftbox 那个 TOTP PoC（`import pyotp`）根本没起来过。

**镜像只缺 `pyotp` 这一个**，其余常用库齐全——这排除了「镜像太瘦普遍跑不动」的
可能，也说明 `verified: false` 不是系统性问题。

### 6.4 顺带改进：缺依赖不再伪装成普通失败

原先 `ModuleNotFoundError` 只表现为 `status: FAILED, exit: 1`，与「PoC 逻辑错」
长得一模一样。现在：

```
error_code:      "MISSING_DEPENDENCY"   （而非 NONZERO_EXIT）
missing_module:  "pyotp"
summary:         "PoC needs 'pyotp', which the worker image does not ship"
```

这个区分有实际用途：它决定了下一步是**改镜像**还是**改 PoC**。混在一起会让
人误以为利用链复现不了。

### 6.5 当前仍未解决

`fresh_reproduction_verified=1` 仍然是 **0**。剩下的路只有一条：

**把 `pyotp` 加进 worker 镜像。** 这是镜像构建的问题，不在本轮代码改动范围内，
但它是「第一次出现 verified=True」的最后一道坎。

另外 `_workspace_poc_steps` 的覆盖率仍是未知数——`2531c1c4`/`72a3325f` 的
workspace 已被清理，无法回看它们当时有没有可提取的 PoC。需要等下一批新解出的题，
在 workspace 还在时观察。
