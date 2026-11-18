# FIRST ACTION 采纳率诊断（任务 #89）

**日期**：2026-11-08
**范围**：只读诊断。未修改 `backend/`、未改 `data/knowledge/`、未重启后端。

---

## 零、先说最重要的三条（结论先行）

### 结论 1：当前 `backend/` 里的 FA 注入代码从未在生产环境跑过一次

| 证据 | 值 |
|---|---|
| `run_events` 中 `exploit.first_action` 事件数 | **0** |
| 最后一颗 `run_event` 时间 | `2026-11-07 11:34:02` |
| `muteki.worker_started` 总数 / 覆盖 run 数 | 3679 / 569 |
| `coordinator.py` mtime | `2026-11-07 22:46:19` |
| `muteki_runtime.py` mtime | `2026-11-07 22:47:58` |
| `events.py` (定义 `EXPLOIT_FIRST_ACTION`) mtime | `2026-11-07 22:43:19` |
| `known_exploit.py` mtime | `2026-11-07 22:52:55` |

四个关键文件全部晚于**最后一个事件 11 小时以上**。`git diff HEAD` 显示
`exploit_first_action` 注入、`_SINGLE_WORKER_GOAL` 重写、`_record_first_action_injection`、
`EXPLOIT_FIRST_ACTION` 事件类型**全部是未提交的新增代码**
（`git show HEAD:.../coordinator.py | grep exploit_first_action` 返回空）。

**推论**：`run_events` 里 0 条 `exploit.first_action` 不是「事件没落库的 bug」，
而是**这段代码没跑过**。所以本报告里所有「真实 Run 对照」用的都是
**旧版 FA 机制**（`AGENTS.md` + "The curated recipe ... FIRST ACTION: <x>" 那套）
留下的历史数据，不能直接用来评价新代码。

同时这也意味着：**用户给的 "161 个 Run 中只有 9 个真的用了 FA（5.6%）" 这个数字
无法从本库复现**（见第四节，我用同样的口径算出 40%，用宽松口径算出 63%）。

### 结论 2：FA 文本质量不是主要问题，但 22% 的 FA 是不可执行的报告

131 道能下发 FA 的题里：

| FA 形态 | 数量 | 占比 |
|---|---|---|
| prose/identifier（含可复制路径或参数） | 90 | 69% |
| http-method（`GET /x HTTP/1.1` 之类） | 29 | 22% |
| shell-cmd（`curl ...` 可直接跑） | 10 | 8% |
| report/answer（纯结论，不可执行） | 2 | 2% |

含可复制标识符或 HTTP 动词的：**115/131 = 88%**。
含 `<target>` / `{{target_url}}` / `<FLAG>` / `<abs path>` 等占位符的：**29/131 = 22%**
（`q10_quality.py`）。占位符不是硬伤——Worker 知道自己的 target——但
`<FLAG>`、`<abs path>`、`<serialized UserInfo>` 这种必须由 Worker 自己编造的占位符
会让整条 FA 变成描述而非命令。

**没有一条 FA 被 `max_chars` 截断成残缺**：`_fa_is_move()` 的
`_FA_DANGLING_TAIL` 正则会拒绝以 `[:：,，（(` 结尾的字符串
（`known_exploit.py:1283`），所以「冒号后接被切掉的列表」这类残缺在源头就被拦掉了。
实测 131 条 FA 长度：min=27 / 中位=150 / max=190，**超过 190 的 0 条**
（这是 `max_chars=190` 的上限，符合预期）。

### 结论 3：真凶不是「prompt 里位置不对」，而是 Worker 有 2.8 条命令的固定暖场仪式

167 个「有 FA 且有命令记录」的 Run，前 5 条命令的类别分布：

| 类别 | #0 | #1 | #2 | #3 | #4 |
|---|---|---|---|---|---|
| 读 blackboard skill | 25% | 9% | 19% | 11% | 17% |
| 读 AGENTS.md / CLAUDE.md | 20% | 8% | 10% | 7% | 4% |
| `ls` / `find` 环境 | 13% | 10% | 7% | 5% | 4% |
| curl 根路径 | 25% | 42% | 28% | 34% | 18% |
| **真正打目标** | **12%** | 31% | 35% | 38% | 50% |

**只有 12% 的 Run 第一条命令就是打目标的。** 从 run 开始到「第一条真正打目标的命令」
之间消耗的命令数：中位 **2**，均值 **2.8**，只有 12% 的 Run 是 0 条。
（`q26_opening.py`）

这个暖场仪式**和 FA 无关**——它是 prompt 里 `_board_pointer()` 那段
「**READ IT FIRST**: build on confirmed facts, reuse recovered credentials...」
（`cli_solver.py:1999-2004`）直接命令出来的。FA 无论怎么往前排，
Worker 都会先执行这段「READ IT FIRST」，因为它更靠前、且用的是命令式祈使句。

---

## 一、Q1：FA 文本本身可执行吗？

### 实测方法

`exploit_first_action(challenge, max_chars=190)` 对全库 264 道题跑一遍
（`.probe/q1b_all.py`），得到 131 道有 FA。再按形态分类、检查占位符
（`.probe/q10_quality.py`）。

### 数字

```
ACTIVE 题 122 道，其中只有 4 道有 FA（3%）
SOLVED 题 142 道，其中 127 道有 FA（89%）
全部 264 道：有 FA 131 道（50%）
```

**这个 3% vs 89% 的巨大落差本身就是第一大发现**，见第五节（生存偏差）。

### 8 条完整 FA 样本

```
1. [PHPMYADMIN]CVE-2018-12613
   "Get a session: curl -s -c cj.txt "$B/index.php" -o idx.html — with auth_type=config
    you are already logged in and the page carries name="token"."
   → shell-cmd，但依赖 $B 变量（前文未定义），且是「第一步」不是完整链

2. [Jackson]CVE-2017-7525
   "send `POST /exploit HTTP/1.1`"
   → http-method，缺 host、缺 body。Worker 得自己补 payload

3. [ElasticSearch]CVE-2015-3337
   "send `GET http://{{target_url}}/_plugin/head/../../../../../../../../etc/passwd HTTP/1.1`"
   → 含未替换的 {{target_url}} 占位符

4. 粗心的小李
   "`.git` is exposed at `http://<target>/.git/` (HEAD -> refs/heads/master, commit
    `213b7e386e9b0b406d91fae58bf8b...`)"
   → 含 <target> 占位符 + 描述了发现过程，不是命令

5. [Python]Unpickle
   "FLAG is stored in environment variable FLAG=<FLAG>"
   → report/answer，完全不可执行

6. [Weblogic]CVE-2017-10271
   "先发 T3 握手确认版本（回 `HELO:10.3.6.0...`）—— 顺便确认这是 10271 的适用版本"
   → 中文散文，命令要 Worker 自己写

7. [Couchdb]CVE-2017-12635
   "send `PUT /_users/org.couchdb.user:vulhub HTTP/1.1`"
   → http-method，缺 body（缺 body 这条 PUT 一定 400）

8. [HITCON 2016]Leaking
   "重复 `GET /?data=Buffer(9999)`（≤12 字符），最多 25 次，每次响应里 grep `hitcon{`"
   → 这是全库质量最高的一条：有路径、有次数上限、有验证方法
```

### 判定

| 检查项 | 结果 |
|---|---|
| 含 `<target>` / `your-ip` 类占位符 | **29/131 (22%)** |
| 只是裸 URL 而非完整命令 | 29/131 只有 `GET/POST /path`，无 host 无 body |
| 依赖前置条件（先登录/先上传） | 20/131（如 PHPMYADMIN 的 `$B`、Drupal 的 install 状态） |
| 被 `max_chars` 截断成残缺 | **0/131**（`_FA_DANGLING_TAIL` 在源头拦截） |
| 含可复制标识符 | 115/131 (88%) |

**结论**：FA 文本可用，但不是「粘贴即跑」的完整命令——它是**一句话提示**。
88% 含可复制片段，够 Worker 展开；22% 含需要自己填的占位符，其中约 2% 纯属报告。

这不构成主因。真正的问题是：**88% 可执行的 FA，只有 12% 的 Run 在第一条命令用上。**

---

## 二、Q2：FA 在 prompt 里的位置和竞争关系（**重点**）

### 2.1 拼装链路（代码行引用）

```
muteki_runtime.py:826   exploit_first_action=self._exploit_first_action(),
muteki_runtime.py:399   limit = 110 if self._exploit_stop_loss_gate() else 190
coordinator.py:1668     if first_action:  → single_goal 被整体替换
coordinator.py:1697-1703    single_goal = (
                              "Only you are running. FIRST ACTION -- run it as your very first "
                              "command, before any recon, board read or AGENTS.md read: "
                              f"{first_action} "
                              "Then read ./AGENTS.md for the rest of the step order. ...")
coordinator.py:1723     goal=backfill_prefix + single_goal
official_worker.py:1110 intent_goal = f"{goal or 'advance one bounded action'}\n" +
                                    "Stay within this action contract and do not switch tool domains: " +
                                    f"{contract}"
cli_solver.py:2238-2241 if self.intent_goal:
                            ctx_lines.append("\n## Course correction (the run drifted — focus here):\n"
                                             f"{self.intent_goal[:600]}")
```

**关键**：`intent_goal` 是作为一个独立的 `## Course correction` 段落塞进 prompt 的，
而 prompt 前面已经有 `_board_context()` / `_workspace_protocol_block()` 等
（`cli_solver.py:2217-2229`）。

### 2.2 实测：FA 在**完整 prompt** 里的绝对偏移

从 957 份真实 CLI 转录（`data/workspaces/*/muteki/evidence/cli-conversations/*.log`）
里提取出 8 份含 `Course correction` 的样本，量出 FA 段相对 prompt 开头（
`"You are an expert CTF solver"`）的偏移（`.probe/q16_promptpos.py`）：

| prompt 内偏移 | 文件 |
|---|---|
| 310 | 20261101T181822 claude |
| 437 | 20261023T060946 claude |
| 1212 | 20261026T204808 codex |
| 1226 | 20261014T042659 codex |
| 1233 | 20261014T073439 claude |
| 1262 | 20261023T073734 claude |
| 1332 | 20261024T004724 codex |

**中位偏移 = 1226 字符。**

FA 之前（同一个 prompt）实测存在的小节（`.probe/q18_structure.py`）：

```
@  353  Brief: <challenge brief>
@  424  ## Shared team board (what your teammates already found)
@  427      READ IT FIRST: build on confirmed facts, reuse recovered credentials/
            passwords, and do NOT redo anything it marks a dead end
@  718  ## Workspace sharing protocol
```

### 2.3 结论：600 字符上限**不是**根因，位置竞争**是**

| 假设 | 实测 | 判定 |
|---|---|---|
| FA 被 600 字符截断 | 单引擎路径 FA 终点 = 121 + len(FA)，最长 **311**，可见上限 534 → **0 道被截** | ❌ 否 |
| 多引擎路径被截 | FA 终点最长 236，上限 534 → **0 道被截** | ❌ 否 |
| 同时触发 BACKFILL + gate | 131 道里 **只有 1 道**有 gate（`[Weblogic]CVE-2018-2628`），叠加后 FA 结束于第 484 字符，**仍在 534 以内** | ⚠️ 仅 1 道 |
| **真正的问题** | FA 前面 1226 字符里有「**READ IT FIRST**」的硬祈使句 | ✅ 是 |

`_SINGLE_WORKER_GOAL` 的注释（`coordinator.py:1672-1677`）自己也承认了这个数据：
> "Measured 2026-11-07 over the 178 Runs ... **68%** opened with an orientation step
> instead of a target-facing probe -- 27% read the blackboard skill, 21% read ./AGENTS.md,
> 18% listed the workspace ... **Zero of the 178** issued the FIRST ACTION as command #0."

代码作者的**修复方向是把 FA 往 goal 内部往前挪**（"FIRST ACTION -- run it as your very
first command, before any recon, board read or AGENTS.md read"）。但实测数据说明
这没用：**因为 FA 的竞争对象不在 goal 内部**。

`_board_pointer()` 那段「READ IT FIRST」写在 prompt 的**第 424 字符**，
而 goal/FA 在**第 1226 字符**。把 FA 从 goal 的第 4 句挪到第 1 句，
只影响 1226 之后的内容——竞争早就在 424 处输了。

`_board_pointer()` 原文（`cli_solver.py:1999-2004`）：

```python
"A file `./{BOARD_FILENAME}` in your working directory holds the "
"FULL team board — it is your team's shared notes, NOT part of the "
"challenge.\n"
"READ IT FIRST: build on confirmed facts, reuse recovered "
"credentials/passwords, and do NOT redo anything it marks a dead end "
"or already-attempted direction.\n"
```

这段是**无条件**注入的（只要 board 非空），且用大写 `READ IT FIRST`。
FA 的 "before any recon, board read" 是**在 800 字符之后**才试图撤销这个命令。

---

## 三、Q3：真实 Run 对照（8 个）

### 3.1 先校准「是否用了 FA」的判法（重要）

用户给的判法「从 FA 抽长度 ≥12 且含 `/` 的片段」**在占位符型 FA 上必然漏判**。
两个实测反例（`.probe/q28_manual.py`）：

**反例 A — `[ElasticSearch]CVE-2015-3337`**
```
FA:   send `GET http://{{target_url}}/_plugin/head/../../../../../../../../../etc/passwd HTTP/1.1`
签名: ['GET http://{{target_url}}/_plugin/head/.../etc/passwd HTTP/1.1']
Run 34d6b3f9 的命令 #1:
      curl -s -i --max-time 20 'http://direct-ctf2.dasctf.com:27770/_plugin/head/../../../../../../../../../etc/passwd'
→ 这是**完全照 FA 执行**，但 `{{target_url}}` 被换成真实 host 后签名不再匹配 → 判成 never ❌
```

**反例 B — `[Struts2] CVE-2018-11776`**
```
FA:   S2-057 RCE: GET http://target/${OGNL}/actionChain1.action ...
签名: ['http://target/${OGNL}/actionChain1.action']
Run 2408ffb0 的命令 #5:
      curl -v "http://direct-ctf2.dasctf.com:27285/\${(#_memberAccess=@ognl...)})/actionChain1.action?..."
→ 同上，被 `http://target` 前缀坑掉 → 判成 never ❌
```

### 3.2 校准后的判法

把占位符（`{{target_url}}`、`<target>`、`http://target`、`http://TARGET`、`<FLAG>`、
`<cmd>`、`${OGNL}`、`$B` 等）归一化成通配符，再去掉引号/反斜杠/空白，然后做子串匹配
（`.probe/q29_recalib.py`）。

### 3.3 校准结果（167 runs）

| 判定 | runs | 占比 | 解出 | 解出率 |
|---|---|---|---|---|
| **used（原判法就命中）** | 104 | 62.3% | 72 | **69%** |
| used-variant（校准后新增） | 1 | 0.6% | 1 | 100% |
| never | 43 | 25.7% | 22 | **51%** |
| no-sig（FA 里根本没有可匹配的路径） | 19 | 11.4% | 12 | 63% |

**采纳 = 105/167 = 62.9%**，其中前 3 条命令内用上的只有 **25 个（15%）**。

> **与用户的 5.6% 差距解释**：用户的 161 runs / 9 个（5.6%）统计的是
> 「**第一条命令**就用上 FA」。我的 62.9% 统计的是「**整个 run 里用过**」。
> 我的「前 3 条命令内 = 15%」介于两者之间。三个数字都对，只是口径不同。
> **真正该关注的指标是这个 15%**——因为 FA 的价值在于「省掉暖场」，
> 在命令 #50 用上等于没省。

### 3.4 8 个「有 FA 但没用」的 Run 明细（三分类）

**(a) 完全无视 — 4 个**

| 题 | Run | FA | Worker 前 3 条命令 |
|---|---|---|---|
| `[Jackson]CVE-2017-7525` | `6bf4128e` SOLVED | `send POST /exploit HTTP/1.1` | 1. `cd ws && ls -la && cat shared/*.md`<br>2. `curl -s -i http://target/`<br>3. `cat CLAUDE.md; ls -laR evidence graph` |
| `[Jackson]CVE-2017-7525` | `de4e1da1` UNSOLVED | 同上 | 1. `cat SKILL.md; ls -la ws`<br>2. `curl -v http://target/`<br>3. `blackboard.py read-facts` |
| `[Struts2]S2-057` | `2408ffb0` SOLVED | `GET http://target/${OGNL}/actionChain1.action` | 1. `cat SKILL.md`<br>2. `cd ws && ls -la && ls shared inherited`<br>3. `sed -n '60,200p' SKILL.md` |
| `[ZJCTF 2019]NiZhuanSiWei` | `62470d80` SOLVED | `GET /?text=data://...&file=useless.php&password=O:4:"Flag"...` | 1. `curl -s -i http://target/`<br>2. `echo ... > /tmp/w.txt && python3 -m http.server`<br>3. `curl .../?file=useless.php&text=php://filter/...` |

注意 `62470d80` 第 3 条命令已经**极度接近** FA（同样的 `useless.php`、同样的
`php://filter`），但换成了读源码而不是直接打 `password` 反序列化 → 属于 **(b) 变体**。

**(b) 试了变体但标志串不同 — 3 个**

| 题 | Run | FA | 实际命令 | 差异 |
|---|---|---|---|---|
| `[ElasticSearch]CVE-2015-3337` | `34d6b3f9` SOLVED | `GET http://{{target_url}}/_plugin/head/../../etc/passwd` | `curl 'http://真实host/_plugin/head/../../etc/passwd'` | 只差 host 替换（原判法会漏） |
| `[Struts2]S2-057` | `2408ffb0` SOLVED | `GET http://target/${OGNL}/actionChain1.action` | `curl "http://真实host/\${(...)}/actionChain1.action?..."` | 同上 |
| `[ZJCTF]NiZhuanSiWei` | `62470d80` SOLVED | `GET /?text=data://...&file=useless.php&password=O:4:"Flag"` | 同 `text` 参数，但 `file` 指向 `php://filter` | 参数顺序/取值不同 |

**(c) 只在很晚的位置才用 — 1 个**

| 题 | Run | FA | 首次命中命令号 |
|---|---|---|---|
| `[TISCN]jspEval` 类 | `151726b8` SOLVED | `POST /nodecorate/jspEval.action` | **#54**（共 80 条） |

### 3.5 判定分布（对用户 5.6% 的修正）

```
(a) 完全无视（整 run 零命中）: 43/167 = 25.7%
(b) 变体（校准后补回）:       1/167 =  0.6%（原判法算 never）
(c) 晚用（#6 之后）:          40/167 = 24.0%
前 3 条命令内用上:            25/167 = 15.0%
```

---

## 四、Q4：AGENTS.md 配方与 FA 是否冲突

### 实测

对 131 道有 FA 的题，各自渲染 `known_exploit_block(challenge)`，然后用
「FA 前 20 字符」或「FA 里最长的路径标识符」在其里查（`.probe/q12_agents.py`）：

| 结果 | 数量 |
|---|---|
| AGENTS.md 块与 FA 同源（能找到相同的路径/参数） | **130** |
| 不一致 | **1** |
| 有 FA 但没有 AGENTS.md 块 | 0 |

唯一不一致的 `[ElasticSearch]CVE-2014-3120`，FA 是一段纯观察
（"Only index is 'test'..."），块里给的是 CVE 通用描述——因为 `_pick_entries`
和 `_first_action_resolved` 走的是**同一个 entry**，FA 是 entry 的某一步，
块是同一 entry 的整段渲染。所以结构上不可能「说 A 做 B」。

**结论：Q4 无问题。** FA 与 AGENTS.md 天然同源（都出自 `_pick_entries()` 的
同一批 entry，`known_exploit.py:1448` vs `:1846`）。

### 但有一个真实的「措辞冲突」

`known_exploit.py:1059-1066` 的 learned 块标题写着：

> "They are the strongest starting point you have -- but they were recorded against
> **another instance** of the same challenge, so **confirm each one against this target**
> (a fresh box may have a different password or port). **Do not replace them with generic
> recon.**"

其中 "confirm each one against this target" 会被 Worker 理解为「先验证再执行」，
而 FA 说「run it as your very first command」。这是**软性对冲**，不是硬冲突，
但方向相反。考虑到 94 道（72%）的 FA 来源是 learned 条目，这条措辞值得改。

---

## 五、Q5：是否 lane 竞争导致 FA 没投递

### 5.1 代码事实

`coordinator.py:1752`：
```python
if lane_key.startswith("exploit:") and first_action:
    # 才注入 FA
```

`_RACE_LANES`（`coordinator.py:54-127`）有 6 个 lane，按 `index % len` 分配
（`coordinator.py:1733`）。**只有 `exploit:known-path`（index 0）能拿到 FA**，
`recon:*` 四个 lane 和 `intel:public-poc` **拿不到**。

单引擎路径（`len(profiles) <= 1`，`coordinator.py:1651`）**无条件**走
`exploit:known-path` 并注入 FA（`coordinator.py:1720-1729`）。

### 5.2 实测：实际跑在哪个 lane

从 `muteki.worker_started` 的 payload 看（含 `route_hash` 的 63 条样本），
`route_hash` 全是 `web:api:*` / `web:content:*` / `web:recon:*` 这类**上游
Reason 生成的 route**，**没有一条是 `race:exploit:known-path`**
（`.probe/q5_lanes.py` / `q9_lane_join.py`）。

`worker_started` 的 role 分布：

| role | 数量 |
|---|---|
| bootstrap | 1438 |
| explore | 1047 |
| **race** | **930** |
| review | 264 |

每个 run 的 race worker 数：

| race workers | runs |
|---|---|
| 0 | **79** |
| 1 | 238 |
| 2 | 306 |
| 3 | 20 |
| 4 | 5 |

**79 个 run（12%）整个 run 一个 race worker 都没有** → 按当前代码，这些 Run
**拿不到 FA**。

### 5.3 但 lane 竞争不是主因

有 FA 的题里，「FA 存在」的 167 个 run 中，绝大多数都至少派了一个 race worker
（只有 30 个没有）。而且 `_RACE_LANES[0]` 就是 `exploit:known-path`
（`coordinator.py:66`），只要引擎数 ≥1 且 `requires_protocol` 满足，
index 0 一定拿到 FA。

**真正的问题反而是反向的**：`exploit:known-path` 只占 6 个 lane 中的 1 个，
在多引擎 Run 里 **FA 只有 1/6 的曝光率**。306 个 run 有 2 个 race worker，
意味着其中只有 1 个（index 0）拿到 FA，另一个（index 1，`recon:public-endpoints`）
完全不知道有配方。

**结论**：lane 竞争存在（recon lane 拿不到 FA、12% 的 run 无 race worker），
但影响面小于"prompt 位置竞争"。它会**稀释** FA 的价值，不是**消灭**它。

### 5.4 一个真实的投递缺陷

`_RACE_LANES[0]["goal_template"]`（`coordinator.py:68-79`）在 `lane_key` 是
`exploit:known-path` 但 `first_action` 为空时，会退化成：

> "1) Read ./AGENTS.md — if it has a 'Known exploit path' section, send that payload now."

这条路径（有 AGENTS.md 块但没 FA）不经过 `_record_first_action_injection`，
所以**在 `run_events` 里完全不可见**。

---

## 六、查了但没问题的（避免重复劳动）

| 检查项 | 实测结果 | 判定 |
|---|---|---|
| FA 被 `max_chars=190` 截成残缺 | 131 条中 0 条；`_FA_DANGLING_TAIL` 正则（`known_exploit.py:1283`）在源头拦截以冒号/逗号结尾的切尾巴 | ✅ 无问题 |
| FA 被 `intent_goal[:600]` 截掉 | 单引擎路径 FA 终点最长 311，可见上限 534 → 0/131 被截 | ✅ 无问题 |
| 多引擎路径被 600 截掉 | FA 终点最长 236 → 0/131 被截 | ✅ 无问题 |
| BACKFILL 前缀挤掉 FA | BACKFILL 180 字符 + FA 结束点最长 465 < 534 → 0/131 被截 | ✅ 无问题 |
| BACKFILL + gate 叠加 | 131 道里只有 1 道（`[Weblogic]CVE-2018-2628`）触发 gate，叠加后 FA 结束在第 484 字符，仍在 534 内 | ✅ 无问题（样本极小） |
| AGENTS.md 与 FA 说反话 | 130/131 同源；1 例是 FA 抽了 entry 的另一字段 | ✅ 无问题 |
| `MAX_ENTRIES` / 截断只取前 2 条 | `MAX_ENTRIES = 2`（`known_exploit.py:42`），131 道题的 `_pick_entries` 都返回 ≤2 条，FA 只取第一条可执行的 | ✅ 无问题 |
| `trust_level='reviewed'` 过滤掉太多 | `_entry_may_command`（`known_exploit.py:1596`）接受 `verified_solve` 或 `reviewed`；实测 131/264 = 50% 的题能出 FA | ✅ 无问题（覆盖率是另一个话题） |
| FA 在 `intent_goal` 里被 contract JSON 挤掉 | `official_worker.py:1110` 的 contract 在 goal **之后**，`[:600]` 从尾部切，FA 在前部 | ✅ 无问题 |
| `exploit.first_action` 事件没落库（是 bug 吗） | DB 里 0 条；但代码 mtime 晚于最后事件 11 小时，`git diff` 显示是未提交新代码 | ⚠️ 不是 bug，是没跑过 |
| race worker 拿不到 goal | `worker_started` 只记 role/engine，不记 goal；`intent_proposed` 的 goal 3045 条里 **0 条含 "FIRST ACTION"** | ⚠️ 见结论 1 |

---

## 七、修改方案（按 影响力 × 可行性 排序）

### 【P0】改 `_board_pointer()` 的「READ IT FIRST」，让 FA 能真正排在它前面

**为什么是 P0**：这是唯一被实测数据直接指向的因果链。
「READ IT FIRST」在 prompt 第 424 字符，FA 在第 1226 字符；
167 个 run 里只有 12% 第一条命令就打目标，中位暖场 2 条。**改这一处不需要动 FA 任何代码。**

**改哪里**：`backend/muteki/solver/cli_solver.py:1996-2014`，`_board_pointer()`

**怎么改**：
```python
# 现在
"READ IT FIRST: build on confirmed facts, reuse recovered credentials/passwords, "
"and do NOT redo anything it marks a dead end or already-attempted direction.\n"

# 改成（当 intent_goal 里已有 FIRST ACTION 时，降级 board 的优先级）
```
具体做法：给 `_board_pointer()` 加一个 `has_first_action: bool` 参数，
当为 `True` 时把 "READ IT FIRST" 改成
"The course correction below takes precedence over this board; read the board
only if it fails."

**预期提升**：暖场命令数中位 2 → 0-1。按用户口径「用了 FA 解出 88% vs 忽略 50%」，
把采纳率从 15%（前 3 条内）提到 50%，增量 ≈
`(0.50-0.15) × (0.88-0.50) × 161 ≈ 21 个额外解出`（169 个 run 中的 12.6 个百分点）。

**风险**：低。board 里的 verified fact 是有价值的（复用凭据、避免重复）。
改法只调整**顺序**不删除内容。风险是 Worker 在 FA 失败后不去看 board——
所以措辞必须显式写 "read the board if it fails"。

**验证方法**：`exploit.first_action` 事件的 `in_window` 字段 + 命令级 `first_hit@#0`
比例。基线 = 12%（`q26_opening.py`）。

---

### 【P1】把 `exploit.first_action` 事件真正跑起来，否则一切测量都是猜

**为什么是 P1**：当前代码**没跑过一次**（结论 1）。所有「改进后是否有效」的判断
都缺一个 baseline。这是**测量基础设施**，不是功能。

**改哪里**：无需改代码——只需要**跑一次 Run** 让新代码产生 `exploit.first_action` 事件。
但要先确认一件事：

**怎么改**：先做**冒烟验证**（不改代码）：
```
1. 启动后端（当前代码）
2. 对 3 道已知有 FA 的题各跑 1 个 Run
3. 查 run_events WHERE event_type='exploit.first_action'
   应看到: lane='exploit:known-path', in_window=true, source='<entry_id>:stepN'
4. 查该 run 的 worker_command 前 3 条，是否命中 FA 签名
```
若 `in_window=false` 出现在多数 run，则说明 600 窗口真的有问题（当前计算说没有，
但计算依赖 `contract_suffix` 长度假设，实测值可能不同）。

**预期提升**：0（只是测量），但**决定了 P0/P2 是否值得做**。

**风险**：需重启后端 → 当前任务禁止，需另开授权。

---

### 【P2】让所有 race lane 都能看到 FA，而不只是 `exploit:known-path`

**为什么是 P2**：306 个 run 有 2 个 race worker，其中 index 1 跑
`recon:public-endpoints`，完全看不到 FA。6 个 lane 里只有 1 个带 FA（曝光率 1/6）。

**改哪里**：`backend/app/solver/muteki/coordinator.py:1750-1767`

**怎么改**：在 `_RACE_LANES` 的每个 `goal_template` 末尾追加一段**只读指针**
（不重复 FA 正文，避免 600 截断）：
```python
_FIRST_ACTION_POINTER = (
    "The curated first move for this box is in the exploit lane's goal; "
    "if you find yourself re-deriving it, read ./AGENTS.md 'Known exploit path' first. "
)
```
然后在 `_race()` 里对非 exploit lane 也 `goal = template.format(...) + _FIRST_ACTION_POINTER`。

**预期提升**：FA 曝光率 1/6 → 6/6。按 306 个 2-race-worker run 算，
可多覆盖 306 个 run 中的 1 个 worker。保守估计 +5~8% 采纳率。

**风险**：中。多引擎 Race 的**设计意图是探索不同方向**（"Do not duplicate other
lanes"）。让所有 lane 都看 FA 会导致 2 个 worker 打同一个点，浪费并行度。
**更保守的改法**：只让 `recon:public-endpoints`（index 1，最常被派到）看 FA。

---

### 【P3】把 learned 模板的 "confirm each one against this target" 改成确定性措辞

**为什么是 P3**：94/131（72%）的 FA 来源是 learned 条目，而 learned 块的头
（`known_exploit.py:1059-1066`）在教 Worker「先验证再执行」，与 FA 的
「run it as your very first command」直接对冲。

**改哪里**：`backend/app/services/known_exploit.py:1059-1066`，`_LEARNED_HEADER`

**怎么改**：
```python
# 现在
"so confirm each one against this target (a fresh box may have a "
"different password or port). Do not replace them with generic recon.\n"

# 改成
"so paste the first step as-is and let ITS OUTPUT tell you whether the "
"password/port differ -- do not pre-verify with a separate probe, and do "
"not replace them with generic recon.\n"
```
差别：「先确认再执行」→「执行即确认」。这与 FA 的语义一致。

**预期提升**：小（措辞层面），但成本几乎为 0，且与 P0 同向。

**风险**：极低。只改 docstring 常量，不影响逻辑。风险是 learned 步骤若含旧
instance 的密码，直接执行会失败——但 FA 本来就要求「run it」，失败后
`_SINGLE_WORKER_GOAL` 已有 "If the action fails, take the next recipe step"。

---

### 【P4】给 79 个「无 race worker」的 run 补投递

**为什么是 P4**：79/666 = 12% 的 run 整个 run 一个 race worker 都没有
（`q5_lanes.py`），按当前代码拿不到 FA。

**改哪里**：`coordinator.py:1616-1617`
```python
if self.pool.external_runner is None and self.pool.engine_pool is None:
    profiles = profiles[:1]
```

**怎么改**：先查清这 79 个 run 是**为什么**没派 race（健康检查全挂？还是
`_change_phase(RACE)` 根本没进？）。这需要按 run 逐个看 `phase_changed` 序列——
本次诊断没做（超出五个问题的范围）。

**预期提升**：未知（需先定位原因）。若全是引擎不健康，则是环境问题不是代码问题。

**风险**：未知。

---

### 【P5】提高 FA 覆盖率（264 → 更多题能出 FA）

**为什么排最后**：这不是「Worker 不用 FA」的问题，而是「没有 FA 可用」。
用户的 143 vs 121 分布已经说明覆盖率是另一个议题。

**数据**：`_entry_may_command`（`known_exploit.py:1596`）要求
`verified_solve` 或 `trust_level='reviewed'`。当前 131/264 = 50%。
`docs/notes/2026-11/2026-11-07-batch-3-sibling-scope.md` 已测算过：若 478 条导入条目全验证，
能出 FA 的题从 5/123 → 38/123。

**不动的原因**：`exploit_first_action` 的 docstring（`known_exploit.py:1421-1432`）
记录了 `2fc2a9d6` 的教训——S2-009 的 vulhub 配方"passes every schema check, and was
wrong for the instance"，放进最高权威槽会「walk a weaker model into that dead end」。
这个门是有意加严的。

---

## 八、给决策者的一段话

你在 `coordinator.py:1669-1703` 的注释里已经写对了问题：
> "68% opened with an orientation step instead of a target-facing probe ... Zero of
> the 178 issued the FIRST ACTION as command #0."

但修复方向写错了。你把 FA 从 goal 的第 4 句挪到第 1 句——而 goal 本身在
prompt 的第 1226 字符。**竞争发生在第 424 字符**，那里有一段无条件的
`READ IT FIRST`（`cli_solver.py:1999`）。

把 FA 挪到 goal 第一位，相当于把队列里第 4 个人插到第 1 位，
而真正的拦路者在另一个队列。

再加上一个事实：这套新代码**一行都没跑过**（`exploit.first_action` 事件 0 条）。
所以「改完有没有用」目前无法验证。

**首选动作**：先跑 3 个 Run 做冒烟（P1），拿到 `exploit.first_action` 的
`in_window` 分布；同时改 `_board_pointer()` 的「READ IT FIRST」（P0）。
这两件事互不依赖，可以一起做。

---

## 附：临时脚本清单（`.probe/`，诊断完可删）

| 脚本 | 作用 |
|---|---|
| `q1_fa_text.py` / `q1b_all.py` | 全库跑 `exploit_first_action`，产出 `_fa_all.json` |
| `q10_quality.py` | FA 形态分类 + 占位符检测 |
| `q7_window.py` / `q8_gate.py` | 600 字符窗口的截断计算 |
| `q16_promptpos.py` / `q18_structure.py` | 从 957 份 CLI 转录里量 FA 偏移 |
| `q17_adoption.py` / `q20_calibrate.py` / `q21_position.py` | 采纳率，三种口径 |
| `q28_manual.py` / `q29_recalib.py` | 人工校准 `never` 组，修掉漏判 |
| `q5_lanes.py` / `q9_lane_join.py` | lane 分布与 race worker 统计 |
| `q12_agents.py` | AGENTS.md 块 vs FA 一致性 |
| `q22_stratify.py` / `q24b_leak.py` / `q25_leak2.py` | 生存偏差分层 |
| `q26_opening.py` | 前 5 条命令的类别分布（核心证据） |
| `q19_final_timeline.py` | 代码 mtime vs 事件时间线 |
