# 2026-11-08 常规题（无 CVE 线索）能力研究 + 落地

针对 90 道既无 CVE 配方、也无正面知识的题。这类题占总缺口的 **69%**，是批量验证完全覆盖不到的部分。

---

## 一、先说结论

| 结论 | 依据 |
|---|---|
| **历史挖掘路线成立** | 清洗后 **331 条真侦察事实**，覆盖 **66 道题**，质量足以直接用 |
| **侦察不是瓶颈** | 52% 的题**已经打到利用阶段**却仍失败 |
| **改 prompt 改不出解题率** | GEPA 实测 3/3 → 3/3，只提效率 +2.6% |
| **真正能改解题率的是架构** | AutoPT 状态机 22% → 41% |

**已落地**：`scripts/mine_box_facts.py` + `known_exploit.box_facts_for()` + runtime 注入。

---

## 二、现状盘点（先确认数据是否存在）

90 道目标题**全部有历史 Run**，没有一道是从没跑过的。

### 2.1 命令级历史只有一半可用

| 状态 | 数量 |
|---|---|
| 有 ≥20 条命令记录 | **50 道** |
| 只有 1-19 条 | 2 道 |
| **完全没有命令记录** | **38 道（42%）** |

那个 0 不是丢数据，是设计如此：观测层只在发出线程有 running loop 时写 `worker_command`，部分执行路径不触发。

**但这不影响挖掘**——`muteki.fact_added` 事件是完整的。以 `资产保修核验平台` 为例，某个 Run 有 **0 条命令**但有 **16 条 fact**。

**fact 是 Coordinator 已经归纳过的结论**，比原始命令流更适合复用，不需要再写「从命令里提炼知识」的 LLM 管道。

### 2.2 这些题卡在哪：侦察不是瓶颈

用命令特征判断历史是否打到过「利用」阶段（`union select`／`../`／`jndi:`／`exec(`／`jwt`／`alg.none`／webshell）：

| 状态 | 数量 | 占比 |
|---|---|---|
| **已打到利用阶段** | **47** | **52%** |
| 只做过侦察 | 5 | 6% |
| 无命令记录 | 38 | 42% |

**这一条直接否掉了「加强侦察纪律」「首页一次性榨取」这类方案——它们只能帮到 6%。**

与外部研究一致：2026 年论文《Autonomous LLM Agents and CTFs: A Second Look》在 30 道新题上的结论是**瓶颈在漏洞识别，不在识别后的执行**（30 道里规划器正确识别 23 道，只有 4 道执行失败）。

---

## 三、18,752 条 fact 里只有 4% 是真侦察

不加筛选数，这 90 道题有 18,752 条长度 >60 字的 fact。分类：

| 类型 | 数量 | 占比 | 例子 |
|---|---|---|---|
| **`REVIEW_NEEDED` 流程噪声** | 11,500 | **61%** | `REVIEW_NEEDED fact_id=56; reason=unverified_or_missing_evidence` |
| **端点遥测 JSON** | 5,338 | **28%** | `{"endpoint": "/help", "request_count": 4, "status_code": 200}` |
| **疑似真侦察** | 896 | **4%** | 见下 |
| 其他/短 | 1,018 | 5% | — |

**89% 是噪声。** 直接灌进知识库的结果是灾难性的——库里 89% 是 `fact_id=56` 这种废字符串。

### 3.1 一个必须警惕的假信号

做跨 Run 相似度时，发现一对 **Jaccard = 1.00**（`8d65283f` vs `eaa450ac`）。查原文：

```
[codex] {"type":"turn.failed","error":{"message":"unexpected status 401
        Unauthorized: Missing bearer or basic authentication in header ..."}}
[codex] {"type":"turn.failed","error":{...401 Unauthorized...}}
```

**两条都是引擎故障记录。** 判据把「同样的报错」当成了「同样的知识」。不清洗就会得到「这个靶机返回 401」这种废物结论。

### 3.2 清洗后的真实库存

排除噪声 + 端点遥测 + 要求含具体信号（`accepts`/`returns`/`credential`/`HTTP \d{3}`/`bypass` 等）：

**331 条真侦察事实，覆盖 66 道题（73%）**

质量抽样（原文）：

```
/api/warranty/check accepts JSON fields `asset_no` and `department`;
valid sample `PC-2026-013`/`OPS` returns `matched:true`

The root page discloses working demo credentials `demo / demo-pass`;
POST /login returns 302 to `/dashboard` with a session cookie.

Legacy path traversal succeeds via /preview?path=public%2F%252e%252e%2Fprivate%2F
review-2026-f5468029.txt; response HTTP 200 and flag{0d55f26130c3b065...

Username enumeration confirmed: `email=admin` → "密码错误" while any other value
→ "用户不存在". Only `admin` is confirmed to exist.

/robots.txt 内容为 `Disallow: /static/secretkey.txt`；该文件存在，内容为
`you-will-never-guess` (20 字节) —— 很可能是 session/JWT/2FA 密钥

POST /api/exports with empty JSON defaults to scope:personal, status:ready,
and returns a download URL.
```

**这些全是下次 Run 能直接用的东西**——端点、参数名、可用凭据、绕过方式，甚至 flag 本身。

---

## 四、跨 Run 重复：这才是被浪费的部分

同一靶机多个 Run 之间，fact 内容的 Jaccard 大量落在 **0.25-0.37**。

`资产保修核验平台` 有 **29 个 Run**，其中 7 个有 fact 的 Run 两两之间重叠全在 0.25-0.37。

**每个 Run 都是白纸开局。** `_order_by_history` 只决定「下一个跑哪道」，从不把历史结论带到下一轮。29 个 Run 里，后面 28 个都在重复发现第 1 个已经写过的东西。

这不是 bug，是设计缺失。

---

## 五、外部研究：四个可直接借用的结论

### 5.1 GEPA 实测：改 prompt 改不出解题率

XPN InfoSec 用 GEPA（遗传-帕累托）优化 CTF agent 的提示词，250 次评估后：

| 挑战 | 基线 | 优化后 | 变化 |
|---|---|---|---|
| 挑战 1（SQLi）| 0.9173 | 0.9121 | −0.5% |
| 挑战 2（反序列化）| 0.8093 | 0.8431 | +4.0% |
| 挑战 3（RFI）| 0.8671 | 0.9175 | +5.0% |

**解题率 3/3 → 3/3，没有变化。** 提升全在轮次/时长的效率分上（评分 75% 权重给轮次）。

但里面几条**工具级纠错**值得抄：

```
curl URL 含 [ 或 ] 必须加 -g / --globoff，否则请求根本没发出去
路径穿越测试用 --path-as-is，避免 URL 归一化吃掉 ../
循环用 [[ ]] 必须显式 bash -lc，不要 /bin/sh
```

第一条正是我们踩过的坑——Joomla `list[fullordering]` 的方括号被 curl 当 glob，第 2、17 步全废。

### 5.2 AutoPT：状态机拆分，22% → 41%

arXiv 2411.01236。核心论点：**只靠 prompt 学习的单 agent 架构能力不够**，需要外部约束。做法是把端到端任务拆成状态，**每个状态独立解决子任务，切换时只把结果报告给下一个状态，不保持整个任务上下文**——防止一个子任务卡死拖垮全程。

五个状态（Agent 态与 Rule 态混合）：

| 状态 | 类型 | 做什么 |
|---|---|---|
| Scanning | Agent | 扫描器拿漏洞列表 |
| **Selection** | **Rule** | 按危害排序，选最可能的漏洞 |
| Reconnaissance | Agent | 定向侦察 |
| Exploitation | Agent | 忠实尝试利用 |
| **Check** | **Rule** | 按输出判断成功与否并跳转 |

实测（GPT-4o mini，20 个实验）：

| 指标 | AutoPT | ReAct |
|---|---|---|
| 任务完成率 | **41%** | 22% |
| 总耗时 | 16,131s | 31,731s |
| API 成本 | **$0.99** | $3.49 |

效率 +96.7%、成本 −71.6%、困难任务完成数近 10 倍。**但绝对水平只有 41%**——即使做了状态机拆分，端到端成功率也只有四成。

### 5.3 反面提醒：CTF 成绩不等于真实能力

BountyBench：**Detect 12.5% / Exploit 67.5% / Patch 90%**。发现漏洞比利用已标识的漏洞难 5 倍以上。

这说明**一旦知道漏洞是什么，执行本身不是主要障碍**——与我们「52% 已打到利用阶段」的观察一致。

---

## 六、已落地的实现

### 6.1 `scripts/mine_box_facts.py`

从 `run_events` 挖 fact，清洗、去重、限额，输出 `data/derived/box-facts.jsonl`。

三个关键常量，每个都是标定出来的：

```python
MIN_FACT_CHARS = 80        # 更短的是一句话标签，下次一个请求就重新发现
MAX_FACTS_PER_BOX = 8      # 12 条时中位 932 但 17 道超 1500，收到 8
MAX_BOX_CHARS = 1400       # 硬上限，宁可少一条也不给半句话
MAX_FACT_CHARS = 220       # 与渲染器每条预算对齐
```

**清洗判据**（`is_usable`）是核心：

- `_JUNK_PREFIX`：`REVIEW_NEEDED`、`Native Worker completed`、`ROUND_BUDGET`、`[codex]{"type":"turn.failed"`
- `_JUNK_CONTAINS`：`401 Unauthorized`、`Missing bearer`、`rate limit`、`timed out`——这几条正是那个 Jaccard=1.00 假信号的来源
- `_TELEMETRY`：`{"request_count": ...}` 这类端点遥测
- `_SIGNAL`：必须命中 `accepts`/`returns`/`endpoint`/`credential`/`HTTP \d{3}`/`bypass` 之一

**去重**按「字母数字归一化后前 120 字符」——两条只有标点和引号不同的 fact 是同一条。

### 6.2 `known_exploit.box_facts_for()`

渲染成独立 section，带**明确的不确定性声明**：

> The observations below were recorded by previous Runs against this exact target. They are **leads, not verified facts** -- earlier Runs also recorded things that turned out to be wrong, and the box may have been rebuilt since. Re-confirm anything you rely on before acting on it.

这条声明不是装饰。历史 fact 里有明确的错误记录（引擎故障被当成观察），不标注就会重演。

### 6.3 接入位置

`muteki_runtime._seed_known_exploit_block()`，顺序是：

```
checklist → deadend discipline → box facts → recipe
```

有配方的题也保留事实块——配方给具体载荷，事实补配方没描述的侦察。渲染器预算已经封顶，配方不会被挤掉。

---

## 七、过程中修掉的两个既有 bug

### Bug 1：learned 块的幂等 marker 失效

`known_exploit_block()` 返回的 learned section 标题是 `## Already paid for on this exact box`，但幂等检查找的是 `## Known exploit path (learned from`——**标题改过，marker 没同步**。

后果：每次 `_seed_known_exploit_block` 都重复追加 826 字符。实测 `NSB_Login` 两次调用产生两份相同 section。

已修：markers 加上新标题（保留旧拼写，因为老 workspace 里还是旧标题）。

### Bug 2：`*` 应为 `**`（严重）

```python
# 错
*self._exploit_first_action_provenance_kwargs(),

# 对
**self._exploit_first_action_provenance_kwargs(),
```

单星号在已有 keyword 参数的调用里解包，会让 Python 把整个调用当成位置参数：

```
MutekiOrchestrator.__init__() takes 3 positional arguments but 4 positional
arguments (and 17 keyword-only) were given
```

后果：**所有 Run 秒失败 `FAILED_ENGINE`**。

而且它触发了代码注释里自己警告的场景：

> the orchestrator's constructor takes explicit parameters and raises ``TypeError`` on an unlisted keyword -- that is documented in ``core/orchestrator.py`` as a hazard that kills the whole Run as ``MUTEKI_RUNTIME_ERROR`` with no traceback.

**诊断过程值得记录**：后端日志只有 8 行 uvicorn 访问日志，没有堆栈。错误信息藏在 `solve_runs.last_error_message` 里（`run_supervisor.py:1557` 有 `str(error)[:4000]`）。之后用「最小构造 OK / 完整构造失败」二分，加上打印运行时签名，才定位到那一行。

---

## 八、验证结果

### 8.1 工程验证

| 项 | 结果 |
|---|---|
| 挖掘产出 | 66 道题 / 331 条 fact / 每道中位 932 字符 / 最大 1395 |
| 全量回归 | **2382 passed**（2311 → 2382，新增 11 条） |
| 注入验证 | `08e343c0` 的 `AGENTS.md` 8399 字节，含事实块 |

实际注入的样例（`[SUCTF 2019]Upload Labs 2`）：

```
- recon artifacts saved under shared/recon-public including endpoint probes and upload index
- func.php POST with submit plus url (empty or static/secretkey.txt) returns 500
  and no diagnostic body
```

### 8.2 ★ 首个真实样本：解出了，且用了挖掘事实

`08e343c0` / `[SUCTF 2019]Upload Labs 2` / **COMPLETED_SOLVED** / 1489 秒 / 115 条命令 / flag `VALID`

**归因证据**（三条独立）：
1. 挖掘事实明确写了 `func.php` + `submit` + `url` + `static/secretkey.txt`
2. 跑的命令里 `func.php` 出现 **49 次**、`submit` 40 次、`secretkey.txt` 3 次
3. **第 13 条命令的探测列表里同时出现 `static/secretkey.txt` 和 `func.php`**——这两个端点的组合不是巧合：

```bash
for p in robots.txt static/secretkey.txt admin.php config.php class.php func.php upload/ ...
```

4. 第 32 条**逐字复现了事实描述的请求**：

```bash
echo "== baseline normal url (SSRF test)"; curl ... -d 'submit=1...'
```

**这道题在跑之前的状态是 UNSOLVED（历史 1 次 Run），现在解出了。**

### 8.3 ★★ 但暖场消耗了 12 条命令

前 12 条命令**没有一条打靶机**：

```
[1] cat muteki-blackboard/SKILL.md
[2] ls -la && ls -laR shared
[3] ls -laR .muteki_control .muteki_accounts graph evidence
[4] ls muteki-blackboard/ && python3 blackboard.py
[5] env | grep -i muteki
[6] ls shared/ && find / -name "*recon*"
[7-12] sqlite3 graph/upstream_shared_graph.db 各种查询
[13] ← 第一条真正打靶机的命令
```

**12 条纯仪器操作，才碰到目标。** 这比 11-08 诊断里「中位暖场 2 条」严重得多，因为这道题有 115 条命令的预算，暖场比例仍占 10%。

这印证了方向三的诊断结论：`_board_pointer()` 的「READ IT FIRST」在 prompt 第 424 字符，比 FIRST ACTION 靠前得多。Worker 会先读黑板、查图、找历史，然后才动手。

**如果把这 12 条压到 2 条，等于白得 10 条命令的预算。**

---

## 九、下一步（按性价比）

### P0：改 `_board_pointer()`，让 Worker 先打目标再读黑板

本次实测：暖场 **12 条**，而 `08e343c0` 总命令 115 条，占 10%。

方案：给 `_board_pointer()` 加 `has_first_action` 参数，为 True 时把
「READ IT FIRST: build on confirmed facts...」
降级为
「The course correction below takes precedence over this board; read the board only if it fails.」

**这一条同时解决方向三的问题**——它不只让 FA 更早被用，也让所有题的暖场缩短。

### P1：给「Selection」补一个 Rule 态

我们已有 4 个 lane 和规则判断，**缺的是 AutoPT 里的那个 Selection Rule 态**——现在由 Reason 模型自由决定方向，没有一条确定性规则「按证据排序选一个」，所以容易在多个方向间摇摆。

AutoPT 花了整套系统才到 41%，而我们缺的可能只是其中一块。

### P2：把 GEPA 的三条工具纠错加进 checklist

```bash
curl URL 含 [ 或 ] 必须加 -g / --globoff
路径穿越测试用 --path-as-is
循环用 [[ ]] 必须显式 bash -lc
```

低成本、高确定性。第一条是我们已经踩过的坑（Joomla `list[fullordering]`）。

### 不建议做

- **加强侦察纪律**：只能帮到 6%（52% 的题已经打到利用阶段）
- **继续扩 CVE 语料**：覆盖 94%，边际收益低

---

## 十、诚实的局限

1. **单样本，不能下结论。** 只有 `08e343c0` 一道题，且它本来只有 1 次 Run 历史。要证明「事实块提升解题率」需要对照组（有/无事实块各跑若干道）。
2. **331 条 fact 未经二次验证。** 它们是历史 Run 的观察，可能过时或错误。注入时已标注「leads, not verified facts」，但没有自动校验。
3. **66/90 覆盖不是 90/90。** 剩 24 道题的历史里确实没有可用事实。
4. **暖场 12 条这个数字可能不典型。** 这道题有 115 条命令预算，比平均宽松；但比例（10%）依然显著。
5. **状态机方案只有论文数据**，没在我们系统上验证过。AutoPT 的 +19pp 是在他们的基准和实现上得到的。
