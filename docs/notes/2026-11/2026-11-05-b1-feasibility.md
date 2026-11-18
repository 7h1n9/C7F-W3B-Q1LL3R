# B1（复现验证）可行性诊断 — 2026-11-05

时段：2026-11-05 10:00 – 11:30
结论：**B1 在当前数据上无法执行，不是代码问题，是四个独立的前提全部不成立。**
`fresh_reproduction_verified` 全库为 0 的原因，比「没人调用」要根本得多。

---

## 一、四个障碍（按发现顺序，每个都足以单独阻断）

### 障碍 1：执行器第一步就抛 `RunnerRetiredError`

`FreshReproductionExecutor.execute()` 第 22 行：

```python
await runner_client.clear_sessions(run.id)
```

而 `runner_client._require_enabled()` 在 `legacy_kali_runner = False` 时直接抛出
`RunnerRetiredError("RUNNER_LEGACY_DISABLED")`。

实测配置：

```
legacy_kali_runner = False
runner_url         = http://192.168.236.128:8091
```

**即使 Kali Runner VM 开着机，这条路径也必然失败。** 之前以为「Runner VM 没启动所以做不了」
把问题看轻了——VM 只是第二层，第一层是配置层面就关掉了。

### 障碍 2：复现器找的步骤类型，生产里一条都没有

`execute()` 在 `valid_candidate` 为空时用传入的 `steps`；有 valid flag 时构造
`{"tool_name": "http_request", ...}`。

实测 169 个 `COMPLETED_SOLVED` Run 的工具调用分布：

| tool_name | 次数 |
|---|---|
| `muteki_native_worker` | 261 |
| `http_request` / `http_session_request` | **0** |

`muteki_native_worker` 的参数只有 `{engine, intent_id, worker_id}`，
**不含实际执行的任何命令**——真正跑过的 shell 在 `run_events` 里，不在 `tool_calls`。

所以即便绕过障碍 1，`selected` 也是空列表，直接得到 `executed=False`。

### 障碍 3：靶机实例已全部销毁

已解 Run 的靶机地址实测（12 个，去重）：

| 响应 | 数量 | 含义 |
|---|---|---|
| `404 Target not found` | 5 | 网关活着，**靶机实例已销毁** |
| `000`（连不上） | 7 | `192.168.236.1` / `127.0.0.1` 型本地端口，随容器一起没了 |

`404` 的那 5 个不能算「存活」。daScTF 的 `http-ctf2.dasctf.com` 是网关，
`Target not found` 是它明确告诉你「这个靶标不在」。**可复现数 = 0。**

最新已解 Run 完成于 2 天前，靶机生命周期明显短于这个回溯窗口。

### 障碍 4：复现的目标本身对不上

`http_request` 复现的是「把同一个请求再打一次，看能不能再拿到 flag」。
但已解 Run 靠的是 `muteki_native_worker` 里的多轮 shell 交互（nmap、gobuster、
python 脚本、sqlmap…），不是单个 HTTP 请求。**即使靶机活着，重放一个请求也复现不了
一个需要 20 步交互的利用**。

---

## 二、所以 `fresh_reproduction_verified = 0` 不是疏漏

之前记的「代码写好了但从没被调用」只对了一半。真实情况是：

1. 没人调用它；
2. 就算调用，第一步就抛 `RunnerRetiredError`；
3. 就算绕过，没有可重放的步骤；
4. 就算有步骤，靶机已经消失。

四层叠加，设计意图（用「干净会话重放」证明 flag 不是猜到的）在**这个部署形态下无法实现**。

---

## 三、实际执行：改为「flag 来源审计」（方向 A）

重放做不到，但**「flag 是不是凭空编的」这个问题可以从落库数据回答**。

脚本：`scripts/audit_flag_provenance.py`（只读，`--mark` 才写标记，从不删除/降级）

### 判定口径

| 判定 | 含义 |
|---|---|
| `PLATFORM_CONFIRMED` | flag 值出现在 `muteki.flag_found` / `flag.candidate_found` 事件里，即宿主自己按 `flag_pattern` 匹配到了 |
| `SEEN_IN_OUTPUT` | 出现在 `worker_command` / `worker_step` 的载荷里 |
| `SEEN_IN_NARRATIVE` | 只出现在 `fact_added` / report 这类模型自己写的文本里 |
| `NO_FLAG` | 已解但没记录任何 flag（数据问题，不算可疑） |
| `NOT_OBSERVED` | 哪儿都没有 —— 唯一的可疑判定 |

### 结果（169 个已解 Run）

```
PLATFORM_CONFIRMED   149
NO_FLAG               20
suspect                0
```

**没有任何一个已解 Run 的 flag 是编造的。** 169/169 都有 `muteki.flag_found` 事件，
且 `verified: true, confidence: 1.0`。

---

## 四、中途我犯的错（必须记下来，因为它差点得出相反结论）

**第一版审计跑出「61/169 从没被观测到、可疑 81 个（48%）」——这个结论是错的。**

两个盲点：

1. **只扫了 `muteki.worker_command` 的 `result_excerpt`**。但 flag 实际记在
   `muteki.flag_found` 和 `muteki.fact_added` 里，那两类事件我一条都没看。
2. **`result_excerpt` 会截断**，以 `…[+2261 chars]` 标注丢弃长度。抽样 1980 条
   里 19% 被截断，flag 完全可以落在被丢掉的尾巴里。

修正后结论从「48% 造假」翻转为「0% 造假」。**教训：一个「查出大量问题」的结果，
先怀疑检查方法，再怀疑数据。**

---

## 五、顺带查出的真问题：19 条题目元数据错误

`flag_pattern` 与实际发放的 flag 格式不符：

```
07d44a1b bestphp's revenge        pattern=CTF2\{[^}]+\}  got=LCTF{...}      (OPEN)
b00a5218 SQL注入-1                pattern=CTF2\{[^}]+\}  got=n1book{...}    (OPEN)
641f7b08 粗心的小李                pattern=CTF2\{[^}]+\}  got=n1book{...}    (OPEN)
dbb4388a BUU XXE COURSE 1         pattern=flag\{[^}]+\}  got=CTF2{...}      (VALID)
```

这**不是作弊信号**。以 `641f7b08` 为例：模型先报 `n1book{git_looks_s0_easyfun}`，
平台规范化成 `CTF2{git_looks_s0_easyfun}` 并接受，两条都 `verified=True`。
真正的问题是**题库导入时 `flag_pattern` 写错了**，会导致这类题在解题时
误判 flag 无效。

19 条里有相当一部分是 `OPEN`/`INVALID`，说明它们本来就没被平台认可，影响有限；
但 `VALID` 的那几条（如 `dbb4388a`）意味着模式是放宽匹配的，值得单独修。

---

## 六、改动清单

- `scripts/audit_flag_provenance.py`：新增，只读审计 + `--mark` 打标。
- `backend/tests/test_audit_flag_provenance.py`：8 条，锁住四种判定口径、
  截断陷阱、以及「flag 原文绝不进报告文件」。
- `backend/app/solver/muteki/worker/official_worker.py`：`MUTEKI_INJECT_HEALTH_FAILURE_ONCE`
  故障注入点（C1 用），默认关闭。
- `backend/tests/test_muteki_coordinator.py`：+2 条 C1 测试。
- `scripts/verify_health_retry.py`：C1 验证脚本。
- `data/audit/flag-provenance.json`：本次审计报告。
- 全量回归 **2169 passed**。

---

## 七、仍然待办

1. **19 条 `flag_pattern` 元数据错误**需要逐条核对原始题目，改 `challenges.flag_pattern`。
   这是独立的小任务，我可以按 `data/audit/flag-provenance.json` 逐条查。
2. 真正的 fresh reproduction 仍无解，需要方向 B（同 Run 内闭环，靶机活着时复现），
   是几天的活，且要先设计容器执行路径绕开 legacy Runner。

---

## 八、19 条 flag_pattern "不匹配" 的核对结论（12:00–12:40）

逐条查完后，**这 19 条不是元数据错误，我上一节判断错了**。

### 为什么不是错误：代码里写着「声明模式是偏好，不是过滤器」

`backend/app/services/flags.py:46-50`：

```python
# Either the challenge's own format or the generic flag shape.  The
# declared pattern is a preference, not a filter: see GENERIC_FLAG_PATTERN.
return _matches_declared(candidate, pattern) or (
    GENERIC_FLAG_RE.fullmatch(candidate) is not None
)
```

`_extract_matches` 也同时扫「声明模式 + 通用 flag 形状」。所以 pattern 与实际
格式不符**完全正常**，不影响提取也不影响提交。

### 真正的机制：pattern 会被导入时统一刷新

`backend/app/services/arena_solve.py:302-331` 的注释写得很清楚：

> This platform issues `CTF2{<uuid>}` for every container instance, whatever
> banner the original event used -- `[34C3CTF 2017]urlstorage`,
> `BUU XXE COURSE 1` and `[ASIS 2019]Unicorn shop` all had a `CTF2{...}` value
> ACCEPTED by the platform.

导入时 `flag_pattern` 会被刷成平台统一 banner，只有 `metadata.flag_pattern_override`
才保留题目自己的格式。这 19 条是**刷新之前跑的 Run**，记录的是旧值。

现状：264 道题里 **229 条已经是 `CTF2{...}`**，只有 35 条还是旧格式。

### 那 35 条旧格式题，分两类，改法相反

| 类型 | 数量 | 说明 |
|---|---|---|
| daScTF 镜像题 | 8 | BUU 系列、WriteSHell、EasySQL、每日流量邮报。平台实发 `CTF2{...}` |
| 本地点靶机 | 27 | `[LOCAL]CVE-*`、`企业*`、`benchmark:*`、密码题。flag 由本地生成，就是 `flag{...}` |

**用户决定：两类都不改。** 理由成立——`flags.py` 的双模式匹配保证现状能工作，
而乱改会把本地点靶机的 flag 判成无效。

---

## 九、只改了那两条无 flag 题

`[5.1.3 摩斯密码][无flag]` 和 `[5.1.4 仿射密码][无flag]` 是离线密码题，
标题自己写着没有 flag，但仍以 `ACTIVE` 状态 + 默认 `flag\{[^}]+\}` 躺在库里，
且各自已经有 `COMPLETED_UNSOLVED` 的 Run。

`arena_queue.py:500` 的注释其实早就点出了这个问题：

> an attachment-only challenge in this catalogue is usually a teaching exercise
> with no submittable flag, so running those first spent the whole batch on
> challenges that could never score

但**没有任何代码执行这个判断**。这次补上。

### 做法

1. **标记**：`challenges.metadata_json` 加 `no_flag_challenge: true`。
   选 metadata 而非新列，因为 `upsert_local_challenge` 用
   `{**existing.metadata_json, ...}` 合并，加的键**能扛住重新导入**，也不需要迁移。
   `status` 不动——它只有 ACTIVE/SOLVED 两个值，语义是别的。
2. **消费**：`arena_queue._candidates()` 过滤掉标记的题。
   脚本 `scripts/mark_flagless_challenges.py`（支持 `--revert`）。

### 为什么在 _candidates 里过滤而不是入队时

这两条在标记存在之前就已经在队列里了；而且队列行可能比 challenge 行活得更久，
所以**找不到 challenge 行不算「无 flag」**——否则一次瞬时的查询失败会静默排空队列。

### 验证

```
_flagless_platform_ids(3 个真实 id) -> 只命中标记的 2 题，普通题不受影响
ArenaQueueScheduler._candidates()   -> PENDING 只剩 [AppWeb]CVE-2018-8715，无无flag题
```

测试 `backend/tests/test_arena_queue_flagless.py` 7 条：标记命中、未标记放行、
**字符串形式 metadata 也能识别**（老行是串、新行是 dict，用 `JSON_EXTRACT`
会静默漏掉一半，所以在 Python 里判断）、候选集外的题忽略、损坏 metadata 不崩、
缺 challenge 行不当作无 flag、空候选集不发查询。

全量回归 **2176 passed**。

---

## 十、尝试真实验证 B1（13:00–14:40）：三次 Run 均未解出

### 实际执行的 Run

| # | 题目 | 结果 | 事件数 | 时长 | 死路原因 |
|---|---|---|---|---|---|
| 1 | `[AppWeb]CVE-2018-8715` | 环境启动失败 ×3 | — | — | 平台侧：`starting` 48s 后必转 `error` |
| 2 | `[Node]CVE-2017-14849` | COMPLETED_UNSOLVED | 154 | ~24min | `RACE_WORKER_TIMEOUT` |
| 3 | `[Solr]CVE-2017-12629-XXE` | CANCELLED | 21 | ~7min | 我改代码触发后端重启，打断 |
| 4 | 同 3 重跑 | COMPLETED_UNSOLVED | 132 | **1647s / 上限 1800s** | `RACE_WORKER_TIMEOUT` |

**B1 仍未验证**：它的钩子只在 accepted 分支执行，没有 accepted 就没有 `arena.fresh_reproduction` 事件。四次 Run 里该事件数均为 **0**（符合设计）。

### 有价值的副产品

**1. C1 第一次拿到真实抖动证据**

Run 3 的引擎体检事件里：

```
codex-cli   healthy=true   HEALTHY
claude-cli  healthy=false  WORKER_HEALTHCHECK_FAILED
```

后端日志里 `probe failed` 出现 **2 次**（`grep -c` 计数），间隔正好是 `_HEALTH_RETRY_DELAY_SECONDS = 8s`。

但这暴露了 C1 的一个真缺陷：**`retrying once` 那句是 `logger.info`，而本部署没有任何日志配置，root logger 停在 WARNING，这行被丢弃了**。结果就是：重试明明发生了，日志里却看起来像没发生——而读这条日志的人，问的恰恰就是"重试有没有发生"。

已改为 `logger.warning` 并加测试锁定（`test_retry_notice_is_logged_at_a_level_this_deployment_survives`）。

**2. `[AppWeb]CVE-2018-8715` 这道题本身坏了**

对照实验：同一 practice ground 下 `[Node]CVE-2017-14849` **10 秒就 `running` + `access_ready=True`**，而 8715 三次都在 48 秒转 `error`。所以不是平台挂了、也不是配额问题，是**这道题的镜像/编排坏了**。它已有 `attempt_count=8`（`outcome_count=0`），说明反复重试都不成。

**3. 未解出的真因是时间预算，不是方向错**

Run 2 的 worker 在读 `/home/kali/pocs/vulhub/node/CVE-2017-14849/README.md`，
Run 4 的 worker 在用 Python 构造原始 XXE 请求（turn 18）。方向都对，但
Run 4 跑到 **1647 秒撞上 1800 秒上限**。

`coordinator.py:50-63` 的注释里早就写明了这个模式：

> Putting recon first meant every Race spent its whole bounded window enumerating
> robots.txt / headers / 403 directories and timed out (`RACE_WORKER_TIMEOUT`)
> with the exploit never attempted -- the facts read like progress while the flag
> stayed untouched.

现在 recon-first 已改为 exploit-first，但**窗口本身没变**。对需要精确构造
payload 的题（XXE、目录穿越），27 分钟仍可能不够。

### 下一步建议

1. **B1 验证不必专门造 Run**：下一次有任何题 accepted 时会自动触发。真要主动验证，
   挑一道「已知 PoC 一行命令就能打」的题（如 `[ElasticSearch]CVE-2015-1427`），
   命中率远高于需要精细构造的 XXE。
2. **`[AppWeb]CVE-2018-8715` 建议标成 broken**，别再浪费尝试次数。
3. **RACE 窗口是否放宽**需要单独评估——直接调大会挤占队列吞吐，得先量命中率随窗口的变化。
