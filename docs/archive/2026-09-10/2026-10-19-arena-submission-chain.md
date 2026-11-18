# 竞技场提交链路：从「本地解出」到「平台记分」

2026-10-19。BUUCTF-Real 的 `[Tomcat]CVE-2017-12615` 第一次被解出：
Run `4e78b4fd` 达到 `COMPLETED_SOLVED`，`flag_candidates` 里
`CTF2{a569d9ab-41f5-4956-a3c7-b48a0917f155}` 是 `VALID`。
但平台 `is_solved: false`，`arena_submissions` 一条记录都没有。

`solve task` 报的是 `too many values to unpack (expected 3)`。本文记录三个真 bug、
一个平台约束，以及为什么「补提交脚本」这条路对容器题走不通。

---

## 1. 判决性 bug：`scalars` 只返回第一列

`arena_solve.py::_count_held_candidates`：

```python
items = await session.scalars(
    select(FlagCandidate.candidate, FlagCandidate.review_state, FlagCandidate.pattern_matched)
    .where(FlagCandidate.run_id == run_id)
)
return sum(
    1
    for candidate, review_state, pattern_matched in items.all()   # ← 崩在这里
    ...
)
```

`AsyncSession.scalars()` 对**多列** `select` 只保留每行的第一列。所以
`items.all()` 给出的是一串**字符串**（即 flag 本身），`for a, b, c in ...` 去拆一个
38 字符的 flag ⇒ `too many values to unpack (expected 3)`。

它只在「有候选行」时触发，也就是**恰好只在解出题的那些 Run 上触发**：

- 未解出 → `flag_candidates` 为空 → `sum()` over 空集 → 返回 0，一切正常；
- 解出 → 命中候选 → 崩在提交之前。

后果是「找到了，但永远交不上去」：崩溃让 `_run_and_submit` 提前退出，flag 留在库里，
而 20 分钟后环境被销毁，`requires_running_target_for_submit` 让任何事后补交都无效。

修法：

```python
rows = await session.execute(select(三列))
return sum(1 for candidate, review_state, pattern_matched in rows.all() ...)
```

全仓库扫过一遍，`session.scalars(select(A.x, A.y, ...))` 只有这一处。

### 1.1 为什么测试是绿的

`tests/test_arena_candidate_review_gate.py` 里的 `_FakeSession` 只有：

```python
async def scalars(self, _statement):
    return _Rows(self._rows)      # 原样返回 3 元组
```

真实驱动不会这样。「fake 与实现错在同一方向」的测试等于没有测试；这个 fake 错在
**另一个方向**，于是它替生产环境担保了一件假事。

现在 fake 显式模拟真实语义，并把这个方向上的一课写进类注释：

```python
async def scalars(self, _statement):
    return _Rows([row[0] if isinstance(row, tuple) else row for row in self._rows])

async def execute(self, _statement):
    return _Rows(self._rows)
```

新增 `test_a_real_length_flag_is_counted_rather_than_crashing`，用的就是那个 38 字符
的 flag 原值 —— 回归被钉在实际形状上，不是钉在某个 mock 的返回形状上。

### 1.2 用真实驱动验一遍

比任何 fake 都硬的证据：拿已经有候选的那次 Run，直接调这个函数。

```
4e78b4fd  held=0  submittable=['CTF2{a569d9ab-41f5-4956-a3c7-b48a0917f155}']
49af30bf  held=0  submittable=['flag{moxiaoxi}']
```

修前这一句必抛 `expected 3`。

---

## 2. 人工判否的候选仍然会被提交

同一函数隔壁的门槛：

```python
if item.review_state == "OPEN" and not item.pattern_matched:
    continue
```

规则只写了「未判决且不符 banner 的，拦下」。`review_state == "INVALID"`（操作员明确判否）
不满足任何一条 `continue`，于是**照常提交**——循环唯一无视的判决就是人的判决。

对 `49af30bf` 那个 `flag{moxiaoxi}`（原始公开题的 flag，平台已经拒过）意味着再浪费一次
平台写入，且永远不可能对。

修法：`INVALID` 先行短路，且优先于 `pattern_matched`（人看过，人的结论优先）。

---

## 3. `submit_arena_flag.py` 拿错了 id

补提交脚本的 `--list-pending` 打印 `SolveRun.challenge_id`，而那是**本地镜像行**的 id：

```
solve_runs.challenge_id → f2f17a4b-...   （challenges.id，本地新 uuid）
challenges.metadata_json.arena_challenge_id → db6bdcc0-...   （平台题号）
```

拿本地 id 去 `get_practice_challenge` / `submit_flag`，平台回
`NOT_FOUND: The requested resource was not found or is not visible.`

危险之处在于**这个错误长得像「题目下架了」**，会把人引向平台而不是引向这一行数据。
修法：`_pending` 走 `Challenge.metadata_json` 取平台题号，取不到就明确跳过并说明原因，
绝不回退到本地 id。

---

## 4. 环境可见性：一次错误的推断，已否定

中途我观察到「Run 进行中，靶机 HTTP 200，但外部脚本的 `get_environment` 和
`submit_flag` 都返 `NOT_FOUND`」，于是写下结论说**平台的环境可见性绑定在创建它的
MCP 会话上**。这个推断是错的，必须记下来，因为它很像真的。

真实原因就是 §3 那个错 id：`f2f17a4b` 根本不是平台题号，所以
`get_practice_challenge` 也返 `NOT_FOUND`——**这一次 404 与租约、与会话作用域都无关**。
决定性反证：用正确题号 `db6bdcc0` 在同一条外部脚本路径上，`submit_flag`
立刻返回 `accepted: true`（§7），此时环境正在被 Solve 循环租着。

真正被证实、且有独立证据的，只有一条：

> `BUUCTF-Real` 的 flag **按实例轮换**（§7.1）。环境销毁后，旧 flag 永久无效。

所以「外部脚本补提交容器题」失败的原因不是「看不见」，而是**环境已经没了 + flag 已经换了**。
是否真的存在会话级可见性，目前**没有证据**，不要再拿它当解释。

教训：`NOT_FOUND` 在平台 MCP 里是个不区分的错误——题号错、环境不存在、资源不可见，
全都是同一句话。遇到它先核对**手里拿的是哪个 id**，再谈别的。

---

## 5. 取消路径顺手验收

当前 Run 跑的是进程内的旧代码（改代码不会热更已加载的模块），所以就地收掉重跑，
比等它 13 分钟后再崩更省时间。按既有规矩走接口而不是杀进程：

```
POST /api/v1/runs/535a523e-8911-4830-bc1c-c7c5360312a5/cancel  → 200
solve_runs.status → CANCELLED
docker ps -a --filter name=muteki-run  → 空
```

`POST` 返回体是整行 Run 快照。注意 Git Bash 下 `curl -x POST -sS | head` 这种管道
偶发收到 SIGTERM，改成 `-o 文件 -w http_code` 就稳定。

---

## 6. 本轮改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/arena_solve.py` | `_count_held_candidates` 改 `execute`；`_load_candidates` 短路 `INVALID` |
| `backend/tests/test_arena_candidate_review_gate.py` | fake 模拟 `scalars` 只取首列；新增 4 项（含 38 字符真 flag 回归、`INVALID` 三项） |
| `backend/scripts/submit_arena_flag.py` | `_pending` 取平台题号；文档补「会话级可见性」边界 |
| `backend/app/api/v1/arena.py` | （上一轮）`logger.exception` + 异常类型前缀 |

测试：`-k "arena or flag"` → **160 passed**。

---

## 7. 全链路验收通过（2026-10-19 07:37）

修完上面三处、重启后端（已加载的旧模块不会热更）后用**平台题号**重新发起
`POST /arena/solve`，Run `e431f7d7` 一次跑通：

| 检查点 | 结果 |
|---|---|
| Run 状态 | `COMPLETED_SOLVED`（07:37:30，约 8 分钟） |
| solve 任务 | `state: finished`（不再 `failed`） |
| 提取的 flag | `CTF2{00be0143-66a5-4067-b442-59a30c6195cc}` |
| 提交结果 | `accepted: true` / `is_solved: true` / `earned_points: 1` |
| `arena_submissions` | `[Tomcat]CVE-2017-12615 … accepted`（07:37:33） |
| 平台侧 `is_solved` | **True** |
| 环境归还 | `active_environments: 0`，`orphan_suspected: false` |

这是「本地解出」第一次变成「平台记分」。

批量路径（`POST /arena/queue/start`）随即也验过：同场地的
`[Webmin]CVE-2019-15107` 在 7 分钟内解出并 `accepted`
（`CTF2{fd96d12f-d8cf-4d38-b795-556c6462c615}`，07:52:00）。
批量走的是 `arena_queue.py` → 同一个 `solve_one`，差别只在多一个 `on_run_created`
回写队列行，所以这次成功同时证明了「单题」和「批处理」两条入口。

### 7.1 flag 是按实例轮换的

同一个挑战，两次环境给出不同的值：

```
4e78b4fd（第一次环境）  CTF2{a569d9ab-41f5-4956-a3c7-b48a0917f155}
e431f7d7（第二次环境）  CTF2{00be0143-66a5-4067-b442-59a30c6195cc}
```

所以「补提交」不只是环境没了的问题——**旧环境抽出来的 flag 本身也永久失效**。
`BUUCTF-Real` 的每题 flag 绑定单次实例，这解释了为什么当初对已销毁环境补投必然被拒。

### 7.2 遗留：末期有一个残留容器

Run 结束后 `muteki-run-e431f7d7` 仍在跑，且 `StartedAt=07:37:31`——**在 Run 变
`SOLVED`（07:37:30）之后 1 秒才启动**，事件流末尾也没有 teardown。

推测是收尾竞态：Coordinator 已 teardown，某个滞后 worker 又懒创建了一次容器，
之后没人再回收。已手工 `docker rm -f` 清掉。目前的兜底是下次启动
`reap_terminal_muteki_containers()`，运行期不做周期性回收（有意）。
下次跑完如果稳定复现，值得在 finalize 之后补一次 teardown 扫描。

顺带发现一条 10-18 的僵尸 Run（`80d72ce4`，状态一直 `RUNNING`），与本次无关。

---

## 8. 批量实战：小批次 4 题的结果（2026-10-19 07:46–08:31）

提交链路修通后，用 `POST /arena/queue/enqueue` 跑了一小批，验证批量路径能独立完成
「建环境 → 解 → 提交 → 记分 → 归还」全程。

| 题 | 结果 | 耗时 | 台账 |
|---|---|---|---|
| `[Tomcat]CVE-2017-12615` | `COMPLETED_SOLVED` | 8 min | `accepted` |
| `[Webmin]CVE-2019-15107` | `COMPLETED_SOLVED` | 7 min | `accepted` |
| `[WordPress]WordPress4.6` | `COMPLETED_UNSOLVED` | 12 min | `no_candidate` |
| `[Weblogic]CVE-2017-10271` | `COMPLETED_UNSOLVED` | 13 min | `no_candidate` |

批量路径（`/queue/enqueue` + `/queue/start`）与单题路径（`/arena/solve`）行为一致，
两者都走同一条 `arena_solve.solve_one`，没有第二条提交实现——这一点由 2 次
`accepted` 各自落在 `arena_submissions` 证实。

### 8.1 两个 UNSOLVED 不是链路问题

- **WordPress 4.6**：race 阶段两个 worker，`codex-cli` 那个跑满 `worker_timeout_seconds=720`
  才返回，`intent_concluded / timed_out`，`flag_found: false`。Run 在 12 分钟内自行
  finalize，没有「一条轮被无限拖住」。
- **Weblogic CVE-2017-10271**：A 路 worker 确实找到了入口，`python_run` 连续成功，
  fact 里写着 `endpoint=/console/css/%252e%252e%252fconsole.portal`——**那是
  CVE-2020-14882 的路径，不是 10271 的**。10271 要打 `/wsl-wsat/CoordinatorPortType`。
  即：模型把「Weblogic 未授权」这个类别对上了一个更著名的近亲 CVE。

第二条是知识层问题（CVE 与利用路径错配），不是运行期问题；它属于 roadmap 里
CAP 系列要解决的「候选 CVE 与版本/路径绑定」缺口。记在这里，不在本轮修。

### 8.2 收尾卡死：根因已定位并修复（原写「偶发」是错的）

`f3d3640c`（WordPress）在 `muteki.run_finished` 落库（08:04:45，`reason: STOPPED`）之后，
Run 状态仍停在 `RUNNING` 达 11 分钟。当时按「4 个 Run 里 1 次」写成偶发——**这个判断错了**。
同一现象在 `2f441fb3`（Weblogic SSRF）上第二次出现，且这次抓到了活体。

`py-spy dump` 的关键一击是**所有线程都 idle**：事件循环空转、没有 worker 线程、没有阻塞。
`run_finished` 已落库却没人写回状态，只能说明写状态的那个协程已经不存在了。

后端日志给出了它死在哪：

```
File "run_supervisor.py", line 1498, in run_background
File "run_supervisor.py", line 1055, in continue_until_terminal
File "run_supervisor.py", line 1391, in _run_muteki
sqlalchemy.exc.MissingGreenlet
```

链条是：

1. `_run_muteki` 的 `except` 里先 `await session.rollback()`；
2. `rollback` 会 expire `run` 上的所有属性，随后读 **`run.id`** 触发懒加载；
3. 此时 session 已经 unwound，没有 greenlet 可用 ⇒ `MissingGreenlet`；
4. 该异常**从 `except` 块内部抛出**，于是逃出 `_run_muteki`；
5. `run_background` **只有 `try/finally`、没有 `except`** ⇒ 生命周期协程直接死亡；
6. `finally` 已经把 `_active_run_tasks` 注销 ⇒ **Run 从此没有任何所有者**，
   连 `POST /runs/{id}/cancel` 都找不到它。

这也是为什么「偶发」是个误导性结论：它只在**出错路径**上触发，而正常路径永远走不到
`rollback`。4 个 Run 里 1 次，是因为 4 个 Run 里只有 1 个走了出错路径。

修了三处，按「异常从哪来 / 谁该兜住」分层：

| 位置 | 修法 |
|---|---|
| `_run_muteki` 异常处理 | 用进入 `try` 之前捕获的 `run_id` 取代 `run.id`，不再有懒加载 |
| `run_background` | 加 `except (Exception, SystemExit)` → 记 traceback → `_close_crashed_run` 把 Run 收成 `FAILED_ENGINE` / `SUPERVISOR_CRASHED` |
| `run_after_user_input_background` | 同一形状的第二个人口，同样补兜底 |

`_close_crashed_run` 用**全新 session**：抛异常的正是原来那个 session。它对已经终态的 Run
不动（崩溃发生在有用工作之后，调用方要看的是那个工作，不是这次失败），对 `WAITING_USER`
也不动——那是唯一一个**人还拥有** Run 的非终态，打成 failed 会烧掉操作员唯一能回去的路。

已安装 `py-spy`（`~/.workbuddy/binaries/python/envs/default/Scripts/py-spy.exe`）。
这次能定案全靠它：「事件流静默」的成因既可能是阻塞也可能是协程死亡，只有栈能区分。

### 8.3 容器残留：根因是「Finalize 的 teardown 跑在容器创建之前」

三批实战里一共出现过三次残留（`e431f7d7`、`59de9927`、`6bcb7b31`），**全部是 SOLVED 的 Run**；
UNSOLVED 的 Run 容器都被正常回收。方向一致，所以不是随机泄漏。

决定性证据来自 `59de9927` 与 `6bcb7b31` 两组的时序：

| Run | Run 终态时刻 | 容器 `StartedAt` | 容器里有什么 |
|---|---|---|---|
| `59de9927` | 16:42:22 `COMPLETED_SOLVED` | **16:42:21.799** | 只有 `runtime_agent` |
| `6bcb7b31` | 17:19:42 `COMPLETED_SOLVED` | **17:19:42.598** | 只有 `runtime_agent` |

两条都是「容器在 Run 已经终态的那一秒才启动」，且启动后**没有任何 worker 进去干活**。
所以它不是"还没来得及回收"，而是**被一个已经不需要它的流程创建出来的**。

根因在 `ensure_container` 的耗时：它要拉镜像、投影账户目录，可以花几分钟，
比一道解得快的题的整个 Race 还长。于是顺序变成：

1. `official_worker.start()` 把 `ensure_container` 丢进线程，容器还没起来；
2. Run 解出 → Coordinator 进 Finalize → `official_worker.stop()` → `teardown_container`；
   **此时没有容器可删，teardown 空转**；
3. `ensure_container` 终于完成，容器出现 ⇒ 没人会再回收它。

这解释了为什么只对 SOLVED 生效：解得快的 Run 才会让第 2 步抢在第 3 步前面。

修了三层，按「谁来负责」分开：

| 层 | 位置 | 作用 |
|---|---|---|
| 已停止就不再供给 | `official_worker.start()` | `stop()` 置 `_stopped`；`start()` 见到就直接返回，不再 provisioning |
| 供给完成即自我撤销 | `official_worker.start()` | `ensure_container` 返回后若 `_stopped` 已被置位，立刻 `teardown_container` 删掉刚建出来的容器 |
| Run 会话收尾兜底 | `run_supervisor._run_muteki` 的 `finally` | 调 `_reap_muteki_container`：先用一次 `docker ps -a` 判断有没有，有才删；列表拿不到（`None`）时**照删**，因为「列不出来」不能当成「没有」 |

三条测试都做了证伪（关掉对应分支后必须失败）：`_stopped` 守卫关掉 → `ensure` 被调用；
`_reap_muteki_container` 三种分支（有 / 无 / 未知）各断言一次。

#### 验证状态

- **UNSOLVED 路径：修复后 3 题容器全部归零**（`6652d1a3`、`d2f4ad76`、`e7e3dfbd`、`7938fa2b`）。
  不过这条路径修复前本来就是干净的，所以它证明的是"没有回归"。
- **SOLVED 路径：本轮没有等到一次现场验证。** 修复后跑的 5 题里 4 题 UNSOLVED，
  唯一解出的 `6bcb7b31` 跑的是修复**前**的代码。
- 判据留给下一次：Run 变 `COMPLETED_SOLVED` 之后 40 秒，
  `docker ps -a --filter name=muteki-run` 必须为空。空了才算这条真的关掉。

### 8.4 吞吐：每题 12 分钟的下界来自 Race 的慢车道

4 题里解出的两题各用 7–8 分钟，解不出的两题各用 12–13 分钟——**差在 Race 阶段**。
py-spy 在 `b42052a7` 上抓到的栈很干脆：

- A 路（`chat_completions`）早就结束；
- B 路 `codex-cli` 阻塞在 `control_receiver.get()`（容器内的 CLI 进程）；
- 主循环 idle 在 `pool.wait()`。

`_await_worker_round` 原本只等 `pool.wait()`——**等全部 worker 结束**，没有「任一 worker
出 flag 就返回」的分支。于是 `worker_timeout_seconds=720` 成了每道题的时间下界：
一条车道已经答完了，另一条还要把整个预算烧完。

按 277 题算：**12 min × 275 ≈ 55 小时**。不解决这个，批量打题不现实。

修法是把「已验证的 flag」也作为一轮的结束条件（`stop_on_flag=True`，只传给 Race）：

```python
round_finished = await self._await_worker_round(timeout=..., stop_on_flag=True)
```

提前返回只停止**等待**，不取消任何 worker——`Finalize` 仍然独占取消与 reap 的顺序，
被放弃的屏障不会让慢车道的进程变成孤儿。安全性上还查了 `_release_completed_semantics()`
有 `active_count` 守卫，不会在还有活跃 worker 时错误地推进语义。

#### 这条测试的第一次是假绿的

`test_race_stops_waiting_once_a_flag_is_verified` 一开始 1.57 秒通过，**把参数关掉照样通过**。
原因在 `MutekiWorkerPool`：当 `external_runner is None and engine_pool is None` 时，`_race`
只会 spawn **第一个** profile——测试里根本没有第二个 worker，`pool.wait()` 立即返回。
生产路径 `external_runner` 非 None，所以测试必须走那条路才有意义。

改成注入 `external_runner` 后证伪成立：参数关掉 → 11.93s 后 `TimeoutError` 失败；
参数打开 → 2.19s 通过。**改动真实有效，测试也真的会失败。**

（同一教训在 §1 出现过一次：`_FakeSession.scalars` 与真实驱动语义相反，替生产担保了一件假事。
本项目的规矩是：新测试必须能被证伪才算数。）

---

## 9. 承前两节的两条遗留（2026-10-20 完成）

§8.3 留了一条「SOLVED 路径没等到现场验证」，§8 末尾留了「WebLogic 知识层错配」与
「writeup 403 + MySQL 1205」。本节三条全部有实测证据。

### 9.1 SOLVED 路径的容器残留：证实存在，且 UNSOLVED 路径也会

上一轮写「UNSOLVED 路径 4 题全部归零」是对的，但据此推断容器问题已解决是**错的**。
本轮拿到两个反例，都是 Run 终态之后容器才启动：

| Run | 终态 | 终态时刻 (UTC) | 容器 StartedAt | 晚 |
|---|---|---|---|---|
| `eaf1de66`（Solr） | COMPLETED_UNSOLVED | 00:00:45 | 00:00:48.743 | **+3.7 s** |
| `8c5e3825`（Supervisor） | **COMPLETED_SOLVED** | 00:08:33 | 00:08:35.564 | **+2.5 s** |

也就是说：**`_stopped` 守卫 + finally 里那一次 `_reap_muteki_container` 都不够**。
原因是容器可能由 A 路 `python_run` 的通道供给，那条路根本不经过 official worker 的
`_stopped`；而单次 reap 与容器创建之间只差几秒，谁先谁后是随机的。

修法是在 Run 收尾后开一个**短且有界的延迟回收窗口**（`_LATE_REAP_DELAYS = (8s, 30s)`）：
窗口内再来两次 `docker ps -a`，见到就 teardown，见到即停。成本是每题两次 docker 调用，
上限 30 秒尾巴；不阻塞 API（跑在 `run_background` 里）。

判据不变：Run 进终态后 40 秒，`docker ps -a --filter name=muteki-run` 必须为空。

### 9.2 writeup 全 403/400：不是「白耗时间」，是「一次 400 就整节报废」

先量了一下：失败本身只花 **2–7 秒**（缓存里有 `started_at/updated_at`），
所以它对墙钟的影响很小。真正的问题是**每一题的叙事章节永久缺失**。

抓到的根因与上一轮的猜测不同。逐个探测 5 个 openai 线配置（真实小请求）：

| 配置 | 端点 | 结果 |
|---|---|---|
| `Kimi-K3` | sub.gavinchen.top | **400** `field temperature invalid, only 1 is allowed for this model` |
| `GLM` | sub.gavinchen.top | 403 `GROUP_DISABLED` |
| `Kimi-K3-wb反代` / `wb-反代*` | 127.0.0.1:8787 | 连接失败 |
| `deepseek-v4-flash` | api.deepseek.com | **200** ✅ |

`Kimi-K3` 排第一。它回的是 `invalid_request_error`，而旧规则把
`invalid_request_error` 当作「我方 payload 缺陷」——**直接终止整条候选链**，
`api.deepseek.com` 连被问的机会都没有。

这个前提本身就是错的：请求体对每个配置逐字节相同，但**参数取值范围是按模型算的**，
不是按请求算的。改了两条：

1. `_parameter_constraint()` 解析 `field <name> invalid[, only <n> is allowed]`，
   同一配置用协商后的值重试一次（`temperature: 0.2 → 1`），失败再往下走；
2. `_is_payload_defect()` 收窄到「拒绝了一个所有 OpenAI 线端点都接受的字段」
   （`messages` / `response_format`），其余一律视为「这台模型的事，换下一个」。

证伪留档：把旧 token 规则套到 Kimi 那条真实 body 上 → `True`（终止整条链）；
新规则 → `False`（继续走）。

### 9.3 `run_scores` 撞 MySQL 1205

> **后续（2026-10-27）：评分机制已整体下线。** 本节及其修复记录保留为历史。
> 下线的直接触发原因就是本节的 1205——重试 4 次在 2026-10-27 夜跑里对 8 条未解出 Run
> 全部耗尽，`_finalize_score_with_retry` 本身也随之删除。

评分故意开自己的 session，所以必然和生命周期事务抢同一行。抢输就是 1205，
而丢一个 `run_scores` 是**静默的**——Run 照样 SOLVED、照样提交，没人发现基准少了一个数据点。

锁等待按定义就是瞬时的（持锁者几毫秒内提交），所以重试而不是丢弃：
`_finalize_score_with_retry()` 最多 4 次，只对 1205/1213 退避重试，其余照旧记日志吞掉。
**不向上抛**：调用方是生命周期路径，缺一个分数不能成为 Run 关不掉的理由。

### 9.4 知识层错配：CVE 身份没有参与检索

`[Weblogic]CVE-2017-10271` 那一轮，检索返回了三条 DES-CTF 的 SSRF 条目，
第一条 score 62.56 —— 而语料里**根本没有 10271 的条目**。BM25 靠什么给的 62？
复现出来：query token 是 `weblogic / cve / 2017 / 10271 / github / com / vulhub / tree / master`，
命中的是「weblogic」+ 一串仓库样板词。也就是说 **vulhub 描述里的 URL 样板就是相关性噪声**，
而语料里最像的「Weblogic SSRF」条目被当成了 10271 的先验 —— Worker 于是去打
CVE-2020-14882 的 `/console/css/%252e%252e%252fconsole.portal`。

两条规则 + 一份数据：

1. **CVE 身份**：条目里写了 CVE 就只对那个 CVE 成立。query 点名了 CVE 而条目点名了**另一个**
   → 直接丢弃（不是降权，是不同漏洞）;点名了同一个 → `+40`，稳居第一。
2. **样板词地板**：`github / com / vulhub / tree / master / raw / readme …`（含复合词拆解）
   单独命中不算相关性，必须至少有一个有信息量的 query token、或一个 CVE 命中、或一个
   显式 metadata 命中。

数据侧新增 `data/knowledge/cve-playbook.jsonl`（`source_type=reference-corpus`，
因此不进 published_at 审计）。当前只收有把握的 4 条：
10271（`/wsl-wsat/CoordinatorPortType`）、2894（`/ws_utc`）、uddiexplorer SSRF、
以及 2628 的**负向条目**（T3/ysoserial 需要原生 socket + JRMP，只有 HTTP 工具的 Worker 做不出来，
早判早收而不是耗光一轮）。

实测（新语料 691 条）：

```
CVE-2017-10271 → 115.27 cve-2017-10271-weblogic-ws-at     ← 正确条目第一
CVE-2018-2894  → 112.62 cve-2018-2894-weblogic-ws-utc     ← 正确条目第一
CVE-2018-2628  → 119.94 cve-2018-2628-weblogic-t3-negative
```

### 9.5 顺带发现：Reason 模型本身是 403

同一轮的事件里有 `provider_error_code: MODEL_PERMISSION_DENIED`（403），
Reason 模型选的正是 `Kimi-K3`（`daf2b404`，sub.gavinchen.top），
分类因此退化成 `SQLI` —— 一个 WebLogic XMLDecoder 题被当成布尔注入去打。
这属于**连接配置问题**（换一个可用的协作模型即可），不是代码缺陷，
但它是 WebLogic 系列连灭的直接原因之一，单独记一笔。

### 9.6 本轮改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/run_supervisor.py` | `_reap_muteki_container` 后接 `_schedule_late_container_reap`（8s/30s 两次复查） |
| `backend/app/services/muteki_llm_report.py` | `_parameter_constraint()`；`_post_completion(payload_overrides=)`；`call_report_model` 每配置两次尝试；`_is_payload_defect` 收窄 |
| `backend/app/services/run_finalizer.py` | `_is_transient_lock_error()` + `_finalize_score_with_retry()`（1205/1213 退避重试，不抛） |
| `backend/app/knowledge/retriever.py` | `cve_ids()`；CVE 命中 +40 / 异 CVE 丢弃；`_is_boilerplate()` 地板 |
| `data/knowledge/cve-playbook.jsonl` | 新增 4 条 CVE→攻击面绑定 |
| `backend/tests/test_supervisor_continuity.py` | +1（延迟回收，已证伪） |
| `backend/tests/test_muteki_llm_report.py` | +2（参数协商 / 协商后的载荷，已证伪） |
| `backend/tests/test_run_finalizer_score_retry.py` | 新增 3 条（已证伪） |
| `backend/tests/test_knowledge_augmentation.py` | +3（CVE 绑定 / 样板词地板 / id 归一化，均已证伪） |

### 9.7 现场验收（新代码，2026-10-20 00:19 重启后）

| 观察 | 结果 |
|---|---|
| `09e059e1`（Solr）COMPLETED_SOLVED，终态后 40s 查容器 | 未长期残留（晚到的容器在约 2 分钟内被清掉） |
| `e4320288`（Weblogic CVE-2017-10271）COMPLETED_UNSOLVED | 容器离场干净 |
| 延迟回收窗口（端到端，真实 docker） | **通过**：容器在 reap 后 3 秒才创建 → 窗口内被 `teardown`，日志 `Reaped a Muteki container provisioned after its Run finished` |
| writeup 参数协商（生产日志） | **生效**：`Kimi-K3 ... rejected the temperature we sent; retrying with 1.0` → 随后 `LLM_REPORT_EMPTY_RESPONSE; trying the next config`（旧行为到此终止，不再往下走） |
| `run_scores` 1205（生产日志） | **生效**：`Solver scoring hit a transient lock wait ... (attempt 1/2/3)` 后不再丢分 |
| 知识层 CVE 绑定（生产事件） | **生效**：10271 那一轮 top-1 从 `des-ctf-87c60b02b2`（Weblogic SSRF, 62.56）变成 `cve-2017-10271-weblogic-ws-at`（115.27） |

#### 但 worker 仍没打对路径 —— 卡在 Reason 模型

同一轮里 `wsl-wsat` 在 worker 步中**零命中**，`console.portal` 出现 3 次。
也就是说：先验已经正确地取到了，**但没有送达 worker** —— Reason 模型 403（§9.5），
协调器拿不到模型输出，先验进不了指令。**这条不修，知识层的修复只到检索层为止。**

修法是一行配置：`arena_connections.default_reason_model_config_id`
`daf2b404`（Kimi-K3 @ sub.gavinchen.top，403）→ `8deec5f3`（deepseek-v4-flash @ api.deepseek.com，实测 200）。
这是本项目里唯一实测可用的 openai 线配置；**它会走外部付费 API**，所以没有擅自改。

#### 另一条：Run 启动时的 1205 会直接废掉一整题

`9f221910` / `0dcc1934` 两题在 1 分钟内 `FAILED_ENGINE`，`last_error_message` 是
`INSERT INTO run_execution_leases` 撞 1205 —— 重启后启动期的事务挤在一起，
新 Run 抢不到锁就整题报废。与 §9.3 是同一类问题，但发生在**开始**而不是收尾：
`run_attempt_service.begin` 目前没有锁等待重试。下一个该补的就是这里。

---

## 10. Kimi 渠道复活之后：Reason 恢复，10271 首次 accepted（2026-10-20）

§9.7 留下的判断是「Reason 模型 403，需要换配置」。用户反馈渠道复活后重新探测，
结论要修正：**Kimi 从来不是"挂了"，是 `temperature` 被拒**。

### 10.1 真因：`temperature` 是按模型协商的，不是按请求固定的

对 `Kimi-K3` 发同一条最小请求，三种取值：

| body | 结果 |
|---|---|
| 省略 `temperature` | **200** |
| `temperature=1` | **200** |
| `temperature=0` | 400 `field temperature invalid, only 1 is allowed for this model` |

而代码里三处硬编码：Reason 规划 `0.3`（上游 `muteki/solver/reason.py`）、
health probe `0.0`、A 路 Worker `0.0`。所以渠道一复活，Reason 就从 403 变成 400，
**每一轮都死在参数上** —— `e4320288` 那轮 `provider_error_code` 全是
`MODEL_PERMISSION_DENIED: provider returned HTTP 4xx` 就是这个。

值得注意的是：`app/engines/openai_compatible.py` 把 400 统一叫
`MODEL_BAD_REQUEST`、消息固定为 "provider rejected the request"，**body 被丢掉了**，
上层因此无从协商。这一条不修，换任何配置都没用。

### 10.2 修法：把参数协商做成共享能力，覆盖全部调用方

| 改动 | 位置 |
|---|---|
| 新增 `parameter_constraint()`，沿 `__cause__` 链找 `httpx.HTTPStatusError` | `app/core/llm_parameters.py`（新） |
| 新增 `is_transient_lock_error()` / 退避 | `app/core/db_retry.py`（新） |
| `_ParameterNegotiatingEngine`：被拒 → 用上游给定的值重试一次 → **记住**，后续调用直接带 | `app/solver/muteki/adapter/reason_model.py` |
| `_parameter_constraint` 改为委托共享实现 | `app/services/muteki_llm_report.py` |
| `_is_transient_lock_error` 改为委托共享实现 | `app/services/run_finalizer.py` |

`_ParameterNegotiatingEngine` 包在 Reason 边界上，不改 vendored 的 `backend/muteki/`，
也不影响 Worker 通道；协商只发生一次，第二次仍失败就照原样抛出（不掩盖真故障）。

### 10.3 顺带修掉：Run 启动期的 1205（§9.7 标记的那条）

`run_attempts.begin` 写 `run_execution_leases` 撞 1205 → 整题 1 分钟 `FAILED_ENGINE`。
实测三次：`9f221910`、`0dcc1934`、以及本轮重启后的 `b76030e8`。
`begin` 拆成 `_begin_once` + 重试外壳（4 次，仅 1205/1213），重试前 `rollback()` 并
**重新 `session.get(SolveRun, run_id)`** —— rollback 会让所有 ORM 实例过期，
直接碰 `run.status` 会在 asyncmy 下触发 `MissingGreenlet`（同 §3 那条老坑）。
`DomainError`（真的已有 lease）不重试。

修复后 `b76030e8` 的下一轮 `22659b96` 正常跑完 11 分钟。

### 10.4 数据错误：playbook 把 `wls-wsat` 写成了 `wsl-wsat`

`CVE-2017-10271` 的攻击面是 `/wls-wsat/CoordinatorPortType`（WLS **Web Services**）。
`data/knowledge/cve-playbook.jsonl` 里 7 处全拼成 `wsl-`。上一轮「worker 步里
`wsl-wsat` 零命中」这句结论本身也带着同一个拼写错误 —— 真正该查的是 `wls-wsat`。

已全部修正，并加了一条可证伪的断言（`test_playbook_paths_match_the_real_endpoints`，
把数据改回 `wsl-` 即失败），防止再次回退。

### 10.5 现场验收：`22659b96`（[Weblogic]CVE-2017-10271）

| 观察 | 结果 |
|---|---|
| `muteki.reason.completed` 的 `provider_error_code` | **3 轮全为 NULL**（修复前每轮都是 `MODEL_PERMISSION_DENIED`） |
| `muteki.intent_proposed` / `intent_claimed` | 存在（修复前 `intent_count=0`） |
| worker 实际请求 | `200 874 /wls-wsat/CoordinatorPortType`（修复前只有 `console.portal`，是 14882 的路径） |
| Run 终态 | **COMPLETED_SOLVED**，05:14:30，用时约 11 分钟 |
| 平台 `submit_flag` | **accepted**（`da3de0ca`，05:14:38） |
| 容器回收 | 容器 05:14:34.578 启动（**晚于终态 4.5 秒**），约 1 分钟内消失 —— 延迟回收窗口在新代码下生效 |

这是本项目第一次解出 WebLogic 系列，也是第 5 个 accepted。

### 10.6 仍未解决

writeup 依旧 `failed`：候选链现在是
`Kimi-K3`（sub.gavinchen.top）→ `GLM`（403 `GROUP_DISABLED`）→ 三个 `127.0.0.1:8787`
反代（连接失败）→ `deepseek-v4-flash`（**402 Payment Required**）。
即**唯一实测 200 的外部端点现在欠费**，本机反代没起。协商逻辑本身已验证生效
（`Kimi-K3 ... rejected the temperature we sent; retrying with 1.0`），
缺的是一个真能用的模型 —— 属于连接配置/账单问题，不是代码问题。

### 10.7 锁重试的回归：`rollback()` 会 expire 一切

加上重试后 `2d8105f2`（2894）换成了一个新的失败：
`MUTEKI_RUNTIME_ERROR: greenlet_spawn has not been called; can't call await_only()`。

原因是 `Session.rollback()` **expire 掉 session 里所有已加载实例**，而 asyncio 下
过期属性不能懒加载 —— `_run_muteki` 在 `begin` **之前**就 `session.get(Challenge, ...)`，
重试一回滚，那个 `challenge` 就废了。**题是死于抢救，不是死于锁等待。**

修法：`_reload_loaded_instances()` —— 重试后遍历 `identity_map` 逐个
`await session.refresh(instance)`（失败即跳过：行被删/临时实例）。
重试中新建的 attempt/lease 在回滚后已不在 map 里，所以刷新的都是调用方自己的对象。
重试必须对调用方**不可见**，否则就是把一个可恢复的失败变成不可恢复的。

### 10.8 仍未解决：锁有时被持有 200 秒以上

2894 在重试修复后仍有一次 `FAILED_ENGINE`（`a7a6be3e`，3 次重试全撞 1205）。
现场取证：

```
information_schema.innodb_trx  →  两个事务 RUNNING 168s / 138s，trx_query = NULL
information_schema.processlist →  连接 10110 Sleep 182s（idle in transaction）
SHOW ENGINE INNODB STATUS      →  事务 6501806 ACTIVE，103 row locks
  当前语句：UPDATE tool_calls SET status='INTERRUPTED' ... WHERE run_id=? AND status IN (...)
```

即存在**空闲但持锁的长事务**，MySQL 默认的 50s 锁等待超时被完整耗尽 4 次。
重试只能救瞬时锁；锁被持 200 秒时它无能为力。下一步要定位的是
「谁开了这个事务而不提交」—— 从 `performance_schema.events_statements_history`
看，那条连接最后是只读的 `SELECT model_configs ...`，所以真正的写发生在更早的语句里，
需要按 thread 抓完整语句历史（或给后端会话开 `expire_on_commit=False` + 显式事务边界审查）。
这属于下一个任务，本轮不继续。

---

## 11. 全量切到 Kimi：端口冲突、probe 协商、角色缺口（2026-10-20 下午）

§10.6 说「缺的是一个真能用的模型」。本轮把 Kimi 真正接进全链路，过程中撞到
三个与模型无关的障碍，全都是**配置/环境的形状问题**。

### 11.1 后端起不来：`0.0.0.0:8000` 被一个 GZCTF 靶机容器占了

`uvicorn` 一直报 `[Errno 10048]`，但 `netstat` 里 8000 只有 `CLOSE_WAIT`/`TIME_WAIT`，
没有 `LISTENING` —— 因为占用者是**容器端口映射**，不在宿主机进程表里：

```
docker ps → decipher   0.0.0.0:8000->80/tcp   (decipher-computer:gzctf)
```

它返回的那张 Werkzeug 风格 404 页曾一度被当成「后端活着但路由没了」。
判据：`docker ps --format '{{.Ports}}'` 要先看，别只看 `netstat`。

处理：后端改 **8001**（`scripts/_run_backend.sh` 里三处走 `${BACKEND_PORT_OVERRIDE:-8001}`，
仍可用环境变量改回）。没有动 decipher 容器 —— 它不是本项目的。

### 11.2 探测本身会被参数卡死，于是「健康检查」成了最大的假故障源

`POST /api/v1/model-configs/{id}/test` 用 `model_configs.temperature`（Kimi 存的是 `0`）
→ 上游 400 → `reachable=false`。而**健康检查失败的引擎会被踢出 worker 池**，
所以「探测失败」不是显示问题，是能力问题。

修法：`_post_probe_negotiating()`（`app/api/v1/models.py`）包住 `_post_probe`：
被拒 → 用上游点名的值重试一次 → 把 `{field: value}` 一起返回，
主 chat 探测写进 `capabilities.parameter_constraints` 对外可见。
无值可替换时直接删掉该字段（`field top_p invalid` 这种情况）。

实测：Kimi-K3 从 `ok:false / reachable:false / MODEL_BAD_REQUEST`
→ **`ok:true / reachable:true / normal_chat:true`（13.3s，92+131 tokens）**。

顺带把数据改对：`model_configs.temperature` 对 Kimi 改成 **1**（上游只接受 1），
省掉每次协商多出的那一个请求。

### 11.3 第三处 MissingGreenlet：score 事件提交把实例搞过期

`_emit_score_event()` 里 `commit()`（失败时 `rollback()`）会让 session 里所有实例过期，
而调用方紧接着就是 `run_score_read(item)` 这种**同步**属性访问 —— asyncio 下过期属性
不能懒加载，于是抛 `greenlet_spawn has not been called`。表现是
`Solver scoring failed for run ...`，分数丢失但不影响解题，所以很容易被忽略。

修法：在 `_emit_score_event` 的 `finally` 里 `session.refresh(item)`（`suppress` 兜底）。
与 §10.7 的 `_reload_loaded_instances()` 是同一类坑的第三处：**任何 commit/rollback
之后，调用方手里的 ORM 对象都不可信。**

### 11.4 角色缺口：`MODEL_CONFIG_ROLE_INVALID`

把 `default_model_config_id` 切到 Kimi 后，2894 立刻失败：
`The selected model is not enabled for Worker execution.`

原因是 `capabilities_json.roles` 只有 `["coordinator_reason"]`（没有 `worker`）。
这是**模型配置层的角色声明**，跟模型能不能跑无关 —— Kimi 本来就能当 Worker，
只是没声明。已改为 `["worker", "coordinator_reason"]`。

注意 `default_reason_model_config_id` **早就是 Kimi**，所以 Reason 一直是它；
本轮才把主模型与 worker 池也切过去（worker 池是**追加**条目，不删原来的）。

### 11.5 本轮现场

| 题 | Run | 结果 |
|---|---|---|
| [WebLogic]CVE-2018-2894 | `8638315a` | 首次**完整跑完 17 分钟** → `COMPLETED_UNSOLVED`，容器已回收 |

对比历史：同一题此前是 `a7a6be3e`（3 次锁重试全撞 1205，`FAILED_ENGINE`）和
`2d8105f2`（greenlet 崩溃）。**能跑满全程本身就是进展** —— 2894 的 ws_utc
利用链需要多轮换壳，之前根本没有机会跑到。

Reason 侧确认生效：`muteki.runtime.reason.model.selected` 的 `model` = **`kimi-k3`**，
知识库命中 `cve-2018-2894-weblogic-ws-utc`。
worker 池体检：`codex-cli:7dc8b94a` healthy=true；`claude-cli:c8b5fc1c` healthy=false。

平台战绩未变：rank 152 / solved 11 / attempt 17。

### 11.6 ★★★★ 改 `default_model_config_id` 是无效的：主引擎由 `worker_selections[0]` 决定

把连接的 `default_model_config_id` / `default_engine_type` 全改成 Kimi，API 读回来
也对（`"default_model_config_id":"daf2b404..."`, `"default_engine_type":"openai_compatible"`），
但**新建的 Run 依然是 `engine_type=claude_cli / model_config_id=c8b5fc1c`**。
重启后端也没用 —— 因为配置本来就是每次从库里读的，问题不在这。

真因在 `app/api/v1/runs.py` 的 `create_run` 尾部：

```python
values["engine_type"]    = worker_selections[0].engine_type
values["model_config_id"] = worker_selections[0].model_config_id
```

**Run 的主引擎被 `default_worker_engines` 的第一条覆盖。** 也就是说
`default_model_config_id` 只用于「取不到引擎列表时」的兜底，对主引擎没有决定权；
`solve_runs.engine_type` 记的就是这个被覆盖后的值（呼应 §5 那句「engine_type 会骗人」——
它不是骗人，它是 worker 池首条的投影）。

所以「换主模型」的正确操作是**调整 `default_worker_engines` 的顺序**，不是改
`default_model_config_id`。已把 Kimi 的 `openai_compatible` 条目提到首位
（`[Kimi(oa), claude_cli, codex_cli, deepseek(oa)]`）。

两类坑的形状是一样的：**写进库的字段 ≠ 生效的字段**，中间有一层投影。
判据不要看 API 读回来对不对，要看 **新 Run 的 `engine_type` / `model_config_id`**。

### 11.7 本轮遗留

- 本轮跑完的三题（2894 / WordPress4.6 / 2628 前两轮）都还是 `claude_cli` 主引擎，
  Kimi 只覆盖了 Reason。首条改成 Kimi 之后的新 Run 尚未产出，需要下一轮验收。
- 「openai_compatible 引擎不进 `prepare.engine.checked` 体检」仍未定位完：
  `_worker_profiles` 确实为它生成 `EngineProfile`（`worker_class="reasoning"`），
  但 workspace 侧只看到 CLI 引擎被 emit，说明 orchestrator 在交给 workspace 之前
  又筛过一层。这与上面那条是两件事，别混。

---

## 12. OpenAI-compatible Worker 为什么"跑了等于没跑"（2026-10-20 晚）

用户看 `abad0594`（[Weblogic]CVE-2018-2628，`engine_type=openai_compatible`）觉得
"Worker 运行不太正常"。查下去是三个问题叠在一起，**第一个是致命的**。

### 12.1 ★★★★ 真因：Worker 通道把 `temperature` 写死成 0.0

A 路 Worker（`app/solver/muteki/worker/openai_compatible.py`）每一轮都是：

```python
response = await client.chat(..., temperature=0.0, max_tokens=4000, ...)
```

而 `kimi-k3` 只接受 `temperature=1`。§10.1 给 Reason 加了参数协商，
**Worker 这条路完全没接上** —— 于是每一轮第一个请求就 400，
异常被 `except Exception` 吞成 `OPENAI_COMPATIBLE_WORKER_FAILED`，
Run 里只留下 `tool_steps: 0, steps_total: 1, worker_result: FAILED`。

现场实测（真配置、真端点，同一条 prompt）：

| 路径 | 结果 |
|---|---|
| 旧：`temperature=0.0` | `400 field temperature invalid, only 1 is allowed for this model` |
| 新：`_chat_turn()` | `tool_calls=['http_request']`，`overrides={'temperature': 1.0}` |

即 **Kimi 早就能当 Worker，是我们在第一轮就把它自己的请求打死了**。

### 12.2 ★★★ 失败码被折叠成 `FAILED`，所以"不正常"无法诊断

同一份事件里，三种完全不同的结局长得一模一样：

| 真实原因 | 旧事件 |
|---|---|
| 端点 403（组被停用） | `tool_steps:0, steps_total:1, FAILED` |
| 参数被拒 400 | 同上 |
| 模型只回了散文、没调工具 | 同上 |

因为 `_result()` 的 `status` 只有 `COMPLETED`/`FAILED` 两个值，
而 `result_code` 藏在 `metadata` 里，`worker_finished` 事件`只`取 `status`。

修法三处：
1. `provider_failure_code(error)`：403/401 → `..._PROVIDER_DENIED`、429 →
   `..._RATE_LIMITED`、5xx → `..._PROVIDER_UNAVAILABLE`、其它 4xx →
   `..._PROVIDER_REJECTED`，无 HTTP 因 → 保留 `..._WORKER_FAILED`。
2. 失败时补一条 `worker_step`，`kind="error"`、`tool=<code>`；无工具调用时补
   `kind="note"`、`tool="NO_TOOL_CALL"`，并把 `finish_reason` / `content_chars`
   写进 `provider_detail`（有界 300 字符）。
3. `_flush_worker_steps(result_code=...)`：`worker_finished` 多一个
   `worker_result_code` 字段，`status` 语义不动（避免动到既有消费者）。

### 12.3 ★★★ Chat-Completions 引擎从来没被体检过

`_check_official_worker_health` 里有一句
`protocol != "chat_completions"` 的过滤 —— 注释写的是"它们走自己的 Worker 协议"，
但**没有另一半**：CLI 引擎在 Sandbox 里跑一轮真实验，openai_compatible 什么都没跑，
`profile.healthy` 默认 `True`。所以一个 403/503、甚至不收 `tools` 的端点，
永远是"健康"的，会一直领 Worker，然后每一轮交回 `tool_steps: 0`。

新增 `app/solver/muteki/worker/chat_health.py::probe_chat_worker()`：用**同一个
`LLMClient`**（因此端点和 wire 解析、参数协商都与真实回合一致）打一个最小的
带 `tools` 请求。判据分三档：

| 探测结果 | healthy | reason_code |
|---|---|---|
| 200 且回 `tool_calls` | ✅ | `HEALTHY` |
| 200 但没回 `tool_calls` | ✅（**只报告，不踢**） | `WORKER_TOOLCALL_UNCONFIRMED` |
| 401/403 | ❌ | `MODEL_PERMISSION_DENIED` |
| 429 | ❌ | `MODEL_RATE_LIMITED` |
| 5xx | ❌ | `MODEL_UNAVAILABLE` |
| 缺 key/base_url/model | ❌ | `WORKER_PROFILE_INCOMPLETE` |

第二档**故意不判死**：能力正常的模型也可能对一句话探测只回散文，
把这种情况当故障会把好引擎踢光。只有"根本服务不了一个回合"才 fail closed。
接入点：`prepare` 阶段（与 CLI 体检同一轮次）+ `_retry_unavailable_engines`
（原来也把 chat_completions 排除了，现在一起重探，因为端点探测不占 Sandbox）。

### 12.4 现场：新探针对真实 Kimi 连打三次

```
attempt 1: healthy=True reason=HEALTHY
attempt 2: healthy=True reason=HEALTHY
attempt 3: healthy=True reason=HEALTHY
```

期间还观察到中转的抖动（同一 body 连打 4 次全是 503 `Service temporarily
unavailable`），所以 `MODEL_UNAVAILABLE` 这一档是必需的：**端点会真的挂**，
而挂的时候旧逻辑照样发 Worker。

### 12.5 本轮未做的事

- **没有重启后端**（用户要求不动正在跑的 `ef434b0b` / NSB_Login）。新代码在
  下一次重启后生效，`abad0594` 那一轮仍然是旧行为。
- 测试 +20：`tests/test_chat_worker_health_and_negotiation.py`，全部逐条证伪
  （关协商 2 条失败 / 关命名 1 条 / 关探活 2 条 / 去掉 `worker_result_code` 1 条）。
  全量 **1782 passed / 0 failed**。
