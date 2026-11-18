# real 类题目：业务面枚举、合并项与超时/取消语义

**证据来源**：`arena` 库中三道 real 类 Run 的全部事件与命令（`afr_1` 141 条命令、`粗心的小李` 47 条、`SQL注入-1` 56 条），
以及 `backend/app/solver/muteki/` 与 `backend/app/orchestration/` 的实际代码路径。全部结论可回溯到具体行号或命令。

---

## 一、结论摘要（先看这四条）

1. **业务面枚举的粒度错了**：系统按「技术类别」（SSRF/上传/SQLi）枚举，而真实系统的问题挂在**业务动词**上
   （`?p=` 是模块名、`?id=` 是记录主键、`/service/token?scope=` 是镜像仓库授权）。三题无一命中技术类别，全部退化成频次猜。
2. **存在明确的功能重叠且未被合并**：`/` 与 `/index.php`、`/?id=` 与 `/index.php?id=` 是同一入口的两种写法，
   却被分别探测 33/19 次与 19/14 次，占掉了 `SQL注入-1` 全部 56 条命令的 58%。
3. **超时与取消在状态层已经区分，在 Muteki 内层没有**：状态机同时定义 `TIMEOUT` 与 `CANCELLED`
   （库中分别 53 次 / 64 次），但 Muteki 的 RACE 超时最终落成 `COMPLETED_UNSOLVED`（132 次），
   **不是 `TIMEOUT`**。这是语义丢失点在报告第四节给出完整定义。
4. **三题都"找到了 flag"却都 UNSOLVED，原因分两层**：它们拿到的是靶机自带的
   `n1book{...}` 静态诱饵（不是平台注入的 `CTF2{uuid}`），**且这三题在 `arena_submissions` 里
   一条提交记录都没有** —— 说明超时后连提交都没发生，与"提交被判否"是两回事。
   **不要为此放宽匹配**（详见第五节，这是本次最重要的一条否决项）。

---

## 二、枚举清单：每个业务场景及其参数含义与约束

### 2.1 `afr_1` — PHP 文件包含型业务

| # | 业务场景 | 入口 | 参数 | 语义（实测证据） | 约束（实测证据） |
|---|---|---|---|---|---|
| A1 | 内容页展示 | `GET /?p=<模块名>` | `p` | 把 `p` 当**模块名**拼接包含 `./<p>` | **无需 `.php` 后缀**：`p=hello` 输出 `hello.php` 的结果 `hello world!` |
| A2 | 同名模块遮蔽 | 同上 | `p` | 包含的是 `flag.php` 而非页面文案 | `p=flag` 返回 `no no no`（flag.php 自身有门禁），真实内容在**源码注释**里 |
| A3 | 源码读取 | 同上 | `p` | `php://filter` 包装器直通 | `p=php://filter/convert.base64-encode/resource=flag` → flag.php 源码的 base64 |
| A4 | 路径穿越 | 同上 | `p` | `./` 前缀可被 `../` 抵消 | `p=hello/../flag` 与 `p=flag` **等价**（同样返回 `no no no`），说明拼接未规范化 |
| A5 | 文档站 | `GET /tasks/`、`/ctf.html` | — | 静态任务说明页（`_sidebar.md`/`README.md`/`1.3.md`） | 纯静态，无参数 |

**参数试探序列（实测 15 个取值，按顺序）**：
`hello` → `$p` → `world` → ``(空)`` → `flag` → `$(python3` → `./flag` → `hello/../robots.txt` → `hello/../hello` → `hello/../flag` → `index/../index` → `hello%2F..%2Fflag` → `hello..` → `hello/` → `../flag`

> 约束结论：`p` 的唯一约束是「不需要后缀」，这是 A1/A2 两次对照得出的，不是猜的。
> 真正取 flag 的路径是 **A3（wrapper）** 或 **A2 的源码注释**，而不是 A4 的穿越（穿越只能到达同一门禁页）。

### 2.2 `粗心的小李` — 镜像仓库型业务

| # | 业务场景 | 入口 | 参数 | 语义（实测证据） | 约束（实测证据） |
|---|---|---|---|---|---|
| B1 | 首页/仓库页 | `GET /` | — | 返回仓库说明页 | 无参数 |
| B2 | 仓库元数据 | `GET /v2/n1book/web-information-git/tags/list` | — | 列举镜像 tag | 需先取 token（B3） |
| B3 | 令牌签发 | `GET /service/token` | `service`、`scope` | 标准 Docker Registry v2 授权接口 | `service=registry.aliyuncs.com`、`scope=repository:<repo>:pull` **两个参数缺一不可**，否则 401 |
| B4 | 镜像层拉取 | `GET /v2/<repo>/manifests/<ref>` | 头部 `Authorization: Bearer` | 拉 manifest → 拉 blob | 依赖 B3 的 token；manifest 里按 digest 逐个取 layer |
| B5 | Git 泄漏 | `GET /.git/{HEAD,index,logs/HEAD,objects/*}` | — | 完整的裸 Git 目录（200） | 需按 commit `213b7e38` → tree `f46fbac4` → blob `1e0db5d9` **手工还原对象链** |

**参数取值（实测）**：
- `service`（2 个）：`registry.aliyuncs.com`、`registry.aliyuncs.com:cn-hangzhou:26842`
- `scope`（1 个）：`repository:n1book/web-information-git:pull`

> 约束结论：这是三题里**唯一一个参数语义完全由协议规范决定**的业务面。
> `service`+`scope` 的组合是 Docker Registry v2 的硬约束，猜不得；一旦拿对，B4/B5 都是确定路径。

### 2.3 `SQL注入-1` — 单表查询型业务

| # | 业务场景 | 入口 | 参数 | 语义（实测证据） | 约束（实测证据） |
|---|---|---|---|---|---|
| C1 | 首页跳转 | `GET /` | — | **302 重定向**到 `/index.php?id=1` | 无参数；任何探测应先跟随后再判断 |
| C2 | 记录详情 | `GET /index.php?id=<主键>` | `id` | 按主键查 `notes` 表，拼接进 `id='$id'` | **无整型转换**（`id=1'` 返回空 → 报错被吞），`id=1'-- -` 恢复第 1 行 |
| C3 | 调试回显 | `GET /index.php?id=1&tips=1` | `tips` | **未文档化的调试开关**，把原始 SQL 回显到页面 | `tips=1` 即生效，**无需认证**；这是三题里唯一"白送的"观测点 |
| C4 | 静态密钥 | `GET /static/secretkey.txt`（由 `/robots.txt` 的 `Disallow` 暴露） | — | 返回 `text/plain` 的 `you-will-never-guess` | HTTP 200，无需认证；但**是诱饵**（见 2.4） |
| C5 | 列数探测 | `GET /index.php?id=<payload>` | `id` | `order by N` 二分列数 | 3 列（2、3 渲染；1 不渲染） |
| C6 | 数据外带 | 同上 | `id` | `union select 1,2,fllllag from fl4g` | `fl4g` 表仅 1 行；列名 `fllllag`（4 个 l） |

**参数取值（实测）**：`id`（11 个）`1`、`1%27`、`1%27-`、`2`、`0`、`-1`、`$i`、`$P`、`$q`、`N`、`$(python3`

> 约束结论：`id` 的约束是「字符串拼接、无类型校验、错误信息不回显」（C2），
> 所以必须走 **布尔/联合**而不是报错注入；C3 的 `tips=1` 让这条路径从盲注降级为可见回显。

### 2.4 三题共有的「诱饵面」（必须单独登记，不能算业务入口）

| 诱饵 | 出现在 | 为什么危险 |
|---|---|---|
| `n1book{...}` 形式的静态 flag | `afr_1`（源码注释）、`粗心的小李`（index.html 旧 blob）、`SQL注入-1`（`fl4g` 表） | **三题全部命中，三题全部是假**。真 flag 是平台注入的 `CTF2{uuid}` |
| `/static/secretkey.txt` = `you-will-never-guess` | `SQL注入-1`、`afr_1` | 两个 Worker 都花了命令去试它当 cookie/UA/签名 key，全部无效（已记入 dead_end） |
| `/robots.txt` 的 `Disallow` | 同上 | 价值仅在于**指向**诱饵，本身不含信息 |

---

## 三、合并项对照：重叠的接口/参数与统一入口

### 3.1 判定方法

以「(路径, 参数) → 是否同一业务语义」为轴，统计实际探测次数与去重后的取值数。
**重叠 = 同一语义被两个不同字面入口反复探测。**

### 3.2 合并项清单

| # | 重叠项 | 证据 | 是否同一业务 | 合并后统一入口 | 迁移建议 |
|---|---|---|---|---|---|
| M1 | `/` 与 `/index.php` | 33 次 vs 19 次（`SQL注入-1`）；`/` 恒 302 到 `/index.php?id=1` | **是**（302 目标即自身） | `GET /index.php?id=<N>` | 探测一律先 `curl -i` 跟 302，把 `/` 视为别名，不单独计为入口 |
| M2 | `/?id=` 与 `/index.php?id=` | 19 次 vs 14 次，**同一参数同一语义** | **是** | 同上 | 同上；这是 `SQL注入-1` 里最大的一笔重复开销（58% 命令） |
| M3 | `/tasks/` 一族（`_sidebar.md`/`README.md`/`nav.md`/`pages/web/*.md`） | 各 1 次，共 8 条 | **是**（同一静态文档树） | `GET /tasks/_sidebar.md` 一次取索引 | 文档站只取 sidebar，按需再取具体页；不要枚举 `$n.md` 这类模板路径 |
| M4 | `B3 service/token` 的两次探测（`service` 与 `scope` 分开试） | 各 2 次，各 1 个取值 | **是**（同一签发请求的两个必填参数） | `GET /service/token?service=<host>&scope=repository:<repo>:pull` | **必须一次带全**；拆开试只会拿到 401，白花两条命令 |
| M5 | `C2 id` 与 `C6 union` 外带 | 19+14 次 `id` 探测 vs 后续 union | **是**（同一注入点的不同阶段） | 统一走 `GET /index.php?id=<payload>` | 把「列数探测 → 联合 → 外带」串成一个脚本，而不是每次一条 curl |

### 3.3 合并带来的收益（可量化）

- `SQL注入-1`：M1+M2 合并后入口从 2 个降到 1 个，重复探测 33+19=52 条命令中的大部分可省，
  **实际有效探测只有 `id` 的 8 个去重取值 + `tips=1`**。
- `粗心的小李`：M4 合并后 `service/token` 从 2 条降为 1 条；B2/B4/B5 是**串行依赖**不能合并，只能排序。
- `afr_1`：A1–A4 共享同一入口 `/?p=`，**不需要合并入口，需要合并为一次对照实验**：
  `p=hello` / `p=flag` / `p=hello/../flag` 三发一组，即可同时确定后缀约束与穿越等价性。

---

## 四、超时与取消：语义定义与差异

### 4.1 现状（代码事实）

| 层 | 超时 | 取消 | 落库状态 |
|---|---|---|---|
| 外层编排 `orchestration/orchestrator.py:832` | `monotonic()-started > max_runtime_seconds` → `RunStatus.TIMEOUT` | `RunStatus(run.status)==CANCELLED` 早退 | 正确区分 |
| 内层 Muteki `coordinator.py:1562-1567` | `asyncio.TimeoutError` → `RACE_WORKER_TIMEOUT` + `_recover_timed_out_workers()` | `request_stop(reason)` → `_stop_requested=True` | **都落 `COMPLETED_UNSOLVED`** |
| 状态机 `state_machine.py:23-24` | `TIMEOUT` 已定义 | `CANCELLED` 已定义 | 库中 53 / 64 次，均由外层写入 |

**缺口**：内层 RACE 超时后，若剩余时间装不下一轮，`_a_round_still_fits()` 为假 → 写
`ROUND_BUDGET_EXHAUSTED_BEFORE_REASON` 并 `break` → Finalize 以 `STOPPED/SOLVED` 收尾 →
状态落到 `COMPLETED_UNSOLVED`。**纯 Worker 超时被归入"跑完了但没解出"，与"被取消"在状态上无法区分。**

### 4.2 语义定义（建议口径）

| 维度 | **超时（TIMEOUT）** | **取消（CANCELLED）** |
|---|---|---|
| **触发者** | 系统（预算/期限） | 人（`request_stop` / API cancel） |
| **意图** | 想继续，但时间到了 | 不想继续了 |
| **证据处置** | **保留并可再读**：Worker 已写入 Blackboard 的 fact 全部有效 | **冻结**：不再读取、不再派生新结论 |
| **Worker 处置** | 回收（`_recover_timed_out_workers` → `cancel_active` 仅对该 job） | 终止全部（`cancel_all_tasks`） |
| **是否可续** | **可以**：Run 可重启新窗口（`MAX_TOTAL_RUNTIME` 提示 restart to open a new runtime window） | **不可以**：终态，`api/v1/runs.py:1106` 将其列入不可再操作集合 |
| **状态** | `TIMEOUT` | `CANCELLED` |
| **返回结果** | 携带 `partial_evidence=true`、已确认 fact 列表、`reason_code=ROUND_BUDGET_EXHAUSTED_BEFORE_REASON` 或 `RACE_WORKER_TIMEOUT` | 携带 `cancelled_by`、截止时的 fact 快照（只读，不再推进） |
| **后续行为** | 允许：换新窗口重跑 / 人工基于 fact 收尾 / 计入超时统计 | 禁止：任何自动重试、任何自动续跑 |
| **计数口径** | 计入「能力失败」分母，**不计入「策略撤回」** | 计入「策略撤回」，**不进能力分母** |

### 4.3 必须守住的红线

**超时触发后，不得走取消的清理路径。** 具体是三条：

1. **不调用 `cancel_all_tasks()`**。超时只回收超期的那一个 job（`cancel_active(job_key)` + `pool.cancel_workers({该 worker})`），
   兄弟 lane 与共享 Sandbox 必须保留——否则「一个 lane 慢」会被放大成「整轮证据全丢」。
2. **不设 `_stop_requested = True`**。该标志的语义是「操作员已撤回」，一旦置位，后续所有 salvage/Reason 都会被拒。
   超时路径应写自己的原因码，而不是借用 `STOPPED`。
3. **不改判为 `CANCELLED`**。已验证的 flag 不会因超时失效；把超时记成取消会让「差一步」的 Run 在统计上消失，
   也会让「因超时而未解出」这一最重要的一类失败无法被单独度量。

---

## 五、本次最重要的一条否决项

**不要做「检测到形似 `xxx{...}` 就收作候选、再循环查下一个」的改动。**

证据：三道 real 题全部产出了 `FOUND_FLAG=n1book{...}` 形式的 fact，`flag_ok()` 依
`challenge.flag_pattern`（本题为 `CTF2\{[^}]+}`）判为**不通过**，Run 记为 UNSOLVED。

**但要看清失败发生在哪一层**：这三题在 `arena_submissions` 里**一条提交记录都没有**（查询为 0 行）。
也就是说，它们不是"提交了假 flag 被平台拒绝"，而是**超时后 RACE 被回收、根本没有走到提交这一步**。
`flag_ok()` 的格式校验只在真有提交时才会生效——这次它甚至没被触发。

这说明两件事：
1. 严格匹配本身没问题（真 flag 形态见下），**不需要放宽**；
2. 真正要修的是**超时后不再有任何推进**（第四节），否则即使 Worker 拿到了真 flag，也可能因为
   "超时那一刻刚好还没提交"而丢失。

- 真 flag 形态（库中已接受的 56 条）：`CTF2{e7013114-ca50-43e0-ad47-81b75dde599d}` —— **平台注入的 UUID**。
- 假 flag 形态：`n1book{afr_1_solved}` / `n1book{git_looks_s0_easyfun}` / `n1book{union_select_is_so_cool}` ——
  **靶机源码里写死的教学答案**，任何人访问都能看到。
- 题库里有 4 种 `flag_pattern`（`CTF2{...}` 82 道、`flag{...}` 27 道、`flag{[0-9a-f]{32}}` 5 道、`DASCTF{...}` 2 道），
  **只有按题目自身的 pattern 判定才成立**。

如果改成「形似即收作候选并继续循环」，后果是：
1. 上述三类诱饵会**持续**被收为「待确认」，把真正的搜索预算耗在假目标上；
2. 一旦某个假 flag 被平台判为 accepted（例如题目 pattern 恰好宽松），会**产出错误的成功记录**；
3. 「找到了但格式不对」与「没找到」将被混为一谈，**超时/未解出的真实原因被掩盖**。

正确的做法是：**保留严格匹配**，但把「发现了形似 flag 但格式不符」**作为一条显式的 negative 事实**记录下来
（它在 `SQL注入-1` 里已经出现过：`/static/secretkey.txt` 被判为 decoy 并写入 dead_end），
这样下一轮既不会重复踩，也不会误判为成功。

---

## 六、落地建议（按优先级）

| P | 事项 | 依据 |
|---|---|---|
| P0 | 把「业务面枚举」从技术类别改为**业务动词 + 参数语义 + 约束**三元组（本次第二、三节即模板） | 三题无一命中技术类别 |
| P0 | 内层 Muteki 超时写 `TIMEOUT` 而非 `COMPLETED_UNSOLVED`，并保留已写 fact | 库中 53 次 TIMEOUT 全来自外层，内层 0 次 |
| P1 | 入口去重：302 别名归并（M1/M2）、静态文档树只取索引（M3）、签发类接口参数一次带全（M4） | `SQL注入-1` 58% 命令耗在重复入口 |
| P1 | 「形似 flag 但格式不符」写入 negative fact，不进入候选队列 | 三题全部命中诱饵 |
| P2 | 为 real 类增设「诱饵面」登记表，与业务入口分开统计 | `/static/secretkey.txt` 被两题白试 |
