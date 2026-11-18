# W-1 / W-2：Worker 够不到靶场、也看不到题目自带的攻击面

> 日期：2026-10-19
> 结论一句话：**benchmark 的「两臂都不解」不是能力问题。靶机地址从未按容器改写（每条探针 `status_code=0`），
> 用例自带的攻击面也从未进入 Worker 提示。两处已修并各有实测证据；修完仍 0 解，因为真正的天花板是
> 每轮 16 次请求的预算 + 没有脚本工具（见 §6）。**

---

## 1. W-1 标尺：先把「题目有多难」测出来

`backend/data/experiments/w1-ruler/extract_token.py`（只用标准库）对 `POST /api/warranty/check`
的 `department` 做盲布尔注入，二分 ASCII 逐字符抽取：

| 指标 | 值 |
| --- | --- |
| token 长度 | 38 |
| 抽取结果 | `flag{93ffaeedc961d108d17fddd66b90487d}`（与靶场 `WARRANTY_FLAG` 逐字一致） |
| 探测次数 | 257 |
| 墙钟 | **3.33 s**（77 req/s） |

⇒ 题目本身 3 秒可解。任何「给了 720 秒还解不出」都不可能是难度或预算问题，只能是链路问题。

> 语法坑（顺手记下）：子查询与比较必须整体括起来 ——
> `(SELECT … LIMIT 1) > 0` 对，`… LIMIT 1 > 0` 会被解析成 `LIMIT (1>0)` 恒 false，
> 现象是「oracle 全假」，和「注入不存在」长得一模一样。

---

## 2. 缺陷 1：靶机地址从未按容器改写（已修）

### 现象
修复前 Run `12e8c375`（`--arms on --repeats 1 --runtime-seconds 900`，用例 `sql-injection-golden`）：

- 484 s / 9 个工具步 / `INCOMPLETE` / 0 flag
- **12 条 fact 全部 `status_code: 0, success: false`**，包括 `/health` —— 而宿主机 curl 该地址是 200

`status_code: 0` 是「请求根本没发出去」。Coordinator 把它读成「这条路没有新端点了」，
于是每一轮都以 `route_exhausted route=… reason=No new endpoint remains on the assigned route` 收尾，
dead_end 越堆越多，最后 0 flag。**看起来完全是能力问题，实际一个包都没打到靶场上。**

### 实测
Worker 跑在 Sandbox 容器里（`muteki-worker:codex-0.147.0`，`--entrypoint python3`）：

| 容器内地址 | 结果 |
| --- | --- |
| `http://127.0.0.1:8383/health` | `URLError [Errno 111] Connection refused` |
| `http://host.docker.internal:8383/health` | **200** |

### 根因
`container_reachable_endpoint()` 早就存在，但**只用在 model base_url 上**（`muteki_runtime.py` 1105 / 1215 行），
靶机 URL 一处都没改写。`official_worker` 里那段 `container_target_host` 改写只在 B 路（`cli_solver`），
且 `container_target_host` 从未被接线（读不到就静默跳过）。

### 修法
`MutekiRuntime._worker_target(worker_backend=None)`：把 `container_reachable_endpoint()` 应用到**靶机**，
替换 6 处 Worker 可见的 target —— `_worker_profiles` 的两处 `MUTEKI_TARGET_URL`、`_reason_provider` 的
strategy `target_url`、两处 intent URL 构造、`_next_observed_endpoint`。
只改写 loopback，公网 / LAN / compose 服务名一律不动，所以 arena 目标不受影响；
`challenges.target_url` 保持平台原值（不落库污染）。

### 验证
- 单测：`tests/test_cli_endpoint_support.py` +5 项（loopback 改写 / 公网不动 / local 后端不动 / 空 target / env 覆盖）
- 实测：同一命令重跑 Run `e087202c`，事实立刻变成
  `{"endpoint": "/", "status_code": 200, "success": true}`、`/health → 200`、`/robots.txt → 404`

---

## 3. 缺陷 2：用例自带的攻击面没有进 Worker 提示（已修）

### 现象
连通性修好后，Worker 拿到了真实的 404/200，却仍不去打声明端点：
它依次摸了 `/search`、`/track`、`/lookup`、`/js/app.js`、`/static/style.css`……
最后只到达 `GET /warranty` 与 `GET /api/warranty`（都 404）—— **用例里写的是 `POST /api/warranty/check`
带 JSON body，它一次都没试。**

而 `challenges.metadata_json` 里一直存着：

```json
"attack_surface": [{"method": "POST", "endpoint": "/api/warranty/check", "parameter": "department"}],
"initial_knowledge": ["… department is concatenated into the SQL string.", …]
```

### 根因
`workspace.py` 写 `AGENTS.md` 时调用 `hint_text(hints_for_challenge(challenge))`，
而 `hints_for_challenge` 只匹配一个硬编码挑战名（`资产保修核验平台`，或端口 `28319`）。
benchmark 挑战叫 `benchmark:sql-injection-golden`、端口 8383 ⇒ 返回 `{"id": "none", "hints": []}` ⇒
`AGENTS.md` 里 `## Authorized Methodology Hints` 是个**空小节**。

### 修法
`_declared_hints()`：没有命中策展提示集时，回落到**该 Challenge 自己声明的** `initial_knowledge` +
`attack_surface`（渲染成 `Declared attack surface: POST /api/warranty/check (parameter: department)`）。
只用平台已为该 Run 存下的数据，不推断任何东西；什么都没声明的挑战仍得到原来的空集。

### 验证
- 新增 `tests/test_methodology_hints.py` 5 项（声明面渲染 / 进 AGENTS.md / 空声明保持空 / 策展集仍优先 / 畸形 metadata 不抛）
- 回归：`test_muteki_workspace` + `test_muteki_runtime_configuration` + `test_solver_methodology_flow`
  + `test_benchmark_experiment` + `test_muteki_blackboard_runtime_fixes` = **37 passed**

---

## 4. 修复前后对照（同一用例、同一模型、同一预算）

| | 修复前 `12e8c375` | 修复后 `e087202c` |
| --- | --- | --- |
| 事实的 `status_code` | 12/12 = **0** | 200 / 404 真实响应 |
| 是否打到靶场 | 否 | 是 |
| 是否命中声明端点 | 否（全站瞎摸） | 否（只到 `GET /warranty`，未用 `POST /api/warranty/check`） |
| 工具步（`worker_step kind=tool`） | 9 | 27 |
| 结果 | INCOMPLETE | INCOMPLETE |

第二列说明：连通性 + 攻击面是**必要**的两步，但都还不**充分**。

---

## 5. 为什么仍然 0 解：请求预算 16/轮 + 没有脚本工具

盲布尔抽取要 257 次请求。而 `openai_compatible` Worker 的约束是：

| 约束 | 位置 | 值 |
| --- | --- | --- |
| 每轮请求预算 | `openai_compatible.py::_http` → `self._request_count > 16` | **16 次/上下文** |
| 每轮模型回合 | `worker_max_turns = min(80, max_agent_steps)` | 80 |
| 可用工具 | `HTTP_TOOLS` | 只有 `http_request` / `http_session_request` / `submit_flag` / `report_dead_end` |

**没有脚本工具、没有 `python_run`、也没有平台已有的 `sql_boolean_compare` / `boolean_config_extract`
/ `oracle_probe_matrix` 专用工具。** 于是「257 次探测」只能靠模型一次一次手发 —— 一轮 16 次就耗尽。
按 16 worker × 16 次 = 256 次算，即使全打在同一串上也是紧贴极限且需要完美协调，实际不可能。

⇒ 这是 W-5（新增）：**给 chat_completions Worker 一个"一次调用跑一个循环"的能力**。
容器里本来就在跑 `python3 -c`（`_http` 用 `run_cli_streaming_container` + 固定脚本），
把「脚本内容」开放给模型即可，沙箱边界不变；或把平台已有的 SQL/oracle 工具接进 `HTTP_TOOLS`。
**落地情况与实测见 §8。**

---

## 6. 对原计划 W-3 / W-4 的复核（两条都不成立，别改）

### W-3「worker_timeout 720 → 180」：不是瓶颈
`e087202c` 在 **512 s 就自己结束了**（预算 900 s），6 个 Worker 轮全部提前收尾。
轮次结束的原因是 Coordinator 没有新 route 可发，不是墙钟不够。此时压低 `worker_timeout` 不会换来更多轮次，
只会把还有话可说的轮次提前掐断。

⚠️ 顺带记住一个**不能碰的推论**：`worker_timeout_seconds = min(720, max_runtime_seconds)`。
把 `--runtime-seconds` 压到 600，`worker_timeout` 也会变成 600，而 `_a_round_still_fits()` 要求
`剩余 ≥ timeout + grace` ⇒ 首轮就被拒发，**一个 Worker 都不会启动**（不是"跑得慢"）。

### W-4「同 (tool, fingerprint) 两次失败即禁重试」：已经实现了，而且更严
`openai_compatible.py::_http` 里已有两道：

1. `endpoint_key in self._visited_endpoint_keys` → 直接返回 `ROUTE_EXHAUSTED`（同一 endpoint+params 永不重发）
2. `self._consecutive_duplicate_requests >= 2` → `self.route_exhausted = True`，本轮收工
3. 再加共享活动锁 `_claim_endpoint_activity()`（跨 Worker 去重）

所以 W-4 不需要新代码；真正卡住 throughput 的是 §5 的 16 次预算，不是重复调用。

---

## 7. 对 CAP-03 的影响

`docs/archive/2026-09-10/2026-10-15-cap03-paired-matrix-assessment.md` §3 的「硬阻塞 C：两臂都不解」**结论作废** ——
当时 `off` 臂 1367 s / 44 step / 21 dead_end / 0 flag，是靶机不可达的产物。

现在的真实状态：
- 可达性 ✅（本文 §2）
- 攻击面可达性 ✅（本文 §3）
- 仍需：W-5（脚本/循环能力）→ 之后才能谈「on 臂能不能解」，再之后才轮到 10 对矩阵。

测量口径另有两处必须记住（否则会重犯本文开头的误判）：
- `solve_runs.tool_call_count` 在 A 路只统计 `muteki_native_worker`（= Worker 派生次数），
  **不是工具调用数**；真实工具步看 `run_events` 的 `muteki.worker_step`（`payload.kind == "tool"`，实测 21~70）。
- 因此 benchmark 汇总里的 `average_tool_calls` 目前测的是「派生了几次 Worker」。要不要改成
  `worker_tool_steps` 是独立的一件事（未做，避免和历史产物不可比）。

---

## 8. W-5 落地：A 路 Worker 的 `python_run`

### 8.1 实现

`backend/app/solver/muteki/worker/openai_compatible.py`：

| 项 | 内容 |
| --- | --- |
| 工具 | `WORKER_TOOLS` 新增 `python_run(code, timeout_seconds)`；`HTTP_TOOLS` 保留为别名 |
| 执行 | `run_cli_streaming_container(..., ["python3", "-c", _PYTHON_RUN_SCRIPT, payload])`——与 `_http` **同一条容器通道**，沙箱边界不变 |
| 包装器 | 写临时文件 → `subprocess.Popen` 真跑 → 自己的墙钟超时（kill）→ 输出落盘再读，`RLIMIT_FSIZE=2MB` 防刷库 → 回一行 JSON |
| 边界 | 代码 ≤ 250 KB；每 Worker 8 次；单次 ≤ 120 s；stdout 回传给模型 12 KB |
| 观测 | 每次调用写一条 `muteki.worker_step(tool=python_run)` + 一条 `fact_added`（含 `exit_code/timed_out/stdout_head/error`） |
| flag | 脚本 stdout 进 `self.records`，因此 `submit_flag` 的「必须来自真实观测」校验对脚本同样成立 |

**为什么不算放宽沙箱**：CLI Worker（claude_cli / codex_cli）本来就能在这同一个容器里执行任意 shell；
A 路只是补上了它缺失的迭代能力。

### 8.2 落地过程中挖出的三个新缺陷

| # | 缺陷 | 证据 | 修法 |
| --- | --- | --- | --- |
| 1 | 脚本 cwd 传了**宿主路径**，容器内不存在 → `FileNotFoundError`，且错误被吞成空输出 | 首个探针 6 次 `python_run` 全 `exit_code=-1`、`stdout` 空 | 载荷不再带 `cwd`，改用 exec 已翻译好的容器 cwd；`detail` 字段把异常文本回传给模型 |
| 2 | `ToolCall.parsed_args()` 对非法 JSON **静默返回 `{}`** | 多行脚本带裸换行 → JSON 非法 → 模型只收到 `PYTHON_CODE_REQUIRED`，于是原样重试 | `_parse_tool_arguments()`：`json.loads` → `json.loads(strict=False)`（正是裸换行这一型）→ 仍失败才回 `TOOL_ARGUMENTS_NOT_JSON` 并附修复提示 |
| 3 | 兄弟 Worker 已占住端点时，`_http` **直接 `route_exhausted=True`** | Run `c9a0ece9`：worker-2 拿到正确的 `warranty-check:sqli` 意图，第一次 `http_request` 撞占位锁，**1 秒收工** | 改成与 visited 分支一致的两振出局，并回 `ENDPOINT_ALREADY_EXECUTED` + 「换 payload 请用 python_run」 |

缺陷 3 特别关键：`_endpoint_key` 刻意忽略 payload 值，于是「同端点换注入串」在去重口径里就是重复请求 ——
**注入类题型天生会被自己的去重规则掐死**，除非它能一次跑完循环。

### 8.3 实测（同一用例 / 同一模型 / 900 s 预算）

| Run | 代码状态 | python_run | 结果 |
| --- | --- | --- | --- |
| `80d72ce4`（对照组） | 只有 cwd 修复 | 11 次，全部 `exit_code=-1` | 中止（已知坏） |
| `c9a0ece9` | cwd 修复 | 7 次，`exit_code=0` 5 次 / `timed_out` 2 次 | COMPLETED_UNSOLVED 587 s |
| `e3391674` | cwd + JSON 恢复 + 去重不再秒杀 | 8 次全部 `exit_code=0` | COMPLETED_UNSOLVED 483 s，0 flag |

`c9a0ece9` 里脚本已能打到注入点并拿到结构化响应，例如
`{"endpoint":"/api/warranty/check","status_code":200,...,"success":true}` 与
`base (200, '{"matched":false,...}')` —— 说明**链路通了，是"没做那一步"，不是"做不到"**。

### 8.4 剩下的真问题：用例声明的攻击面到不了 A 路

`e3391674` 的 `muteki.intent_proposed` 原文（Reason 模型写的目标）已经把**技术路线**写对了：

> "Get a matched:true baseline out of POST /api/warranty/check and use it as a boolean/LIKE injection
> oracle ... tautology/contradiction pairs such as `x' OR ...`"

但它把参数猜成了 `serial / warranty_id / product_id / asset_id`，而真正可注入的是 `department`，
`asset_no` 必须是 `PC-2026-013`。这两条事实在 `challenges.metadata_json.initial_knowledge` 里写着，也已经被
`methodology_hints._declared_hints()` 渲染出来了 —— **但只写进了 workspace 的 `AGENTS.md`**，
那是 CLI Worker 才读的文件。A 路（chat_completions）的 `_initial_prompt` 与 Reason 的 `knowledge_prior`
都不含这段内容。

⇒ 新工作项 **W-6**：把 `_declared_hints()` 的输出同时接到
① A 路 `OpenAICompatibleWorker._initial_prompt`，② `CoordinatorReasonModel` 的 `knowledge_prior`。
两臂（on/off）都拿同一份挑战声明，所以不影响 CAP-03 的对照口径。
