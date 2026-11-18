# 常规题能力：文献对照、实测诊断与解决方案

> 2026-11-10 02:20 编写。数据来自本库 `ctf_agent` 实测；文献为 2024–2026 已发表工作。
> 所有量化结论都标注了来源，未验证的推断单独标记。

## 一、先校准：我们到底处于什么水平

### 1.1 公开基准的真实水位

| 系统 | 发表 | 基准 | 成功率 |
|---|---|---|---|
| NYU CTF Baseline | NeurIPS'24 | 200 题 | 4–5% |
| EnIGMA | arXiv 2409.16165 | 200 题 | **13.5%** |
| D-CIPHER | 2025 | 200 题 | 19%（无 auto-prompter 22%） |
| CRAKEN (Self-RAG) | arXiv 2505.17107 | 200 题 | 21.0% |
| CRAKEN (+Graph-RAG, SOTA) | 同上 | 200 题 | **22.0%** |
| CTF-Dojo-32B（微调开源模型） | ICML'26 | 658 题 | 31.9% |
| Claude 3.5 Sonnet（裸模型） | 同上 | — | 37.2% |
| PentestEval 端到端流水线 | arXiv 2512.14233 | 346 任务 | **31%**（自主 agent 几乎全失败） |

**关键参照：最强公开 CTF agent 在 200 题基准上是 22%。**

### 1.2 我们的实测（正确口径）

| 类别 | 已解 / 尝试过 | 解题率 |
|---|---|---|
| 有 CVE（有配方） | 53 / 53 | **100%** |
| 常规题（无 CVE） | 19 / 25 | **76%** |

### 1.3 这两个数字不能直接比较，原因有三

1. **题目难度分布不同**。NYU CTF Bench 覆盖 crypto/pwn/rev/forensics/misc，含大量高难题；我们的库以 **Web 为主**，Web 在文献里普遍是高分区（CRAKEN：web 15.8%，pwn 17.9%，rev 33.3%）。
2. **我们有配方注入**。CVE 题 100% 的原因是 `cve-playbook.jsonl` 里有实测配方——这不是模型能力，是知识注入。文献里 CRAKEN 靠同类手段也只到 22%，因为它的语料是**通用 writeup**，不是逐题实测配方。
3. **我们的常规题暴露在极少数样本上**。19/25，n=25，95% 置信区间约 [55%, 91%]，跨度极大。

**结论：不能宣称「我们比 SOTA 强」。能说的是——在有配方时我们远超公开水平；在无配方时我们与文献同量级，且样本不足以细分。**

## 二、文献指出的核心机制，逐条对照我们的数据

### 2.1 知识注入位置：执行层 > 规划层

**CRAKEN 实测**：注入执行器 21.0% vs 注入规划器 17.0%；web 类 15.8% vs 10.5%。
作者解释：规划是高层抽象，靠模型内生能力；执行需要细粒度、与环境绑定的具体信息。

**我们的现状**：知识目前主要注入 **lane goal（规划层）**。
- `technique_candidate_goal()` 生成一条「No recipe matched. Send the confirming exploit request...」放进 goal
- `known_exploit_block()` 写进 `AGENTS.md`（CLI Worker 读，Chat Completions Worker **不读**）

**已确认的通道分裂**（`methodology_hints.py:106` 的注释自己写了）：

| 通道 | 载体 | 谁读得到 |
|---|---|---|
| `hint_text()` → `workspace.py:107` | `AGENTS.md` 文件 | 仅 CLI Worker |
| `brief_lines()` → `muteki_runtime.py:372` | `_challenge_brief` | Chat Completions Worker |

而 `hints_for_challenge()` 对**6 道缺口题全部返回空**（实测，见 §3.2）。

⇒ **我们两条通道对常规题都是空的，等于零注入。** 这比「注错了层」更严重。

### 2.2 语料形态：叙述性 writeup > 零散 payload

**CRAKEN 实测**：writeup 语料 21.0%，payload 库 16.0%，代码片段 17.5%，三者混合 15.5%。
⇒ 混合会**变差**。作者归因：混合未经策展，引入噪声。

**CTF-Dojo 实测**（ICML'26）：加 writeup 提示使解题数从 168 → 217，**+7.4%**；Crypto/Rev/Misc 收益最大。

**我们的现状**：`cve-playbook.jsonl` 是逐题结构化配方（接近 writeup），`des-ctf.jsonl` 656 条来自外部知识库，`injection.jsonl` 10 条通用技法。
但 **`technique_candidates` 只收 `kind=technique`**，1476 条 `kind=poc` 全被排除；且语料技法名与探针名不一致（`OS command injection` vs `Command injection and RCE`）。

**我昨天尝试修这个（别名表归一化），结果无效**——计数从 9 涨到 398，但四道不同题的候选列表一字不差。原因是排名按语料条目数，一个类压倒性领先时扩输入只放大它。**已回滚。**

⇒ 这条文献结论成立，但**对我们是无效路径**：我们不缺语料条数，缺的是「针对这道题的观察」。

### 2.3 长程衰减与上下文污染

**HORIZON / Chroma Context Rot**：200K 窗口在未填满前就可能损失 30–50% 准确率；结构化输入比打乱输入衰减更快。
**Adaline**：每步 2% 错误率，20 步累积后失败率 33%。
**Arize**：多轮退化主因是 context overflow、注意力稀释、工具错误累积、摘要丢失。

**我们的实测对照**（`[BUUCTF 2018]Online Tool` run `af47e4d8`）：

```
总命令 264，涉及目标 1 个，唯一前缀 114
  70x  curl -sS "http://<target>/..."
  46x  /bin/bash -lc "cd /tmp; U='http://<target>..."
```

**50% 的命令与另一条共享前 60 字符。** 这就是文献说的「endless tool loops while treating broken output as constructive progress」。

### 2.4 我最想验证的一条：失败了却不自知

`996game`（run `825b24c1`）的 fact 里有：

```
NEED_INPUT=the `init-world` handler/Server.js wiring is not exposed and
HTTPS egress for upstream source/writeups is blocked (TLS EOF)
```

**我实测该 Worker 容器出网**：

```
https://raw.githubusercontent.com/... -> 200 (0.69s)
https://github.com                    -> 200 (0.72s)
https://api.github.com                -> 200 (0.46s)
https://www.google.com                -> 200 (0.73s)
```

**网络是通的。** 这条 `NEED_INPUT` 是误判，Worker 据此放弃了查资料。

但——**我随后自查了全部 22014 条 fact**，只有 33 条含「网络受限」类表述，且抽样看大多是**真实的靶机端口不可达**（WebLogic 28050、T3 连不上），还看到 Worker 正确验证过 `egress is working (google:200, github:200)`。

⇒ **这是个案，不是系统性误判。** 我差点又凭一个样本下错结论——这与昨晚两次「凭形态猜病因」是同一个错误。记录在此以免重犯。

### 2.5 循环检测：文献阈值 vs 我们的实测

**AutoGPT 社区实践**（awesome-agent-failures）：

| 参数 | 推荐值 |
|---|---|
| 重复动作阈值 | **3 次** |
| 交替循环阈值 | 2 个完整周期 |
| 无进展窗口 | 10 个动作 |
| 冷却期 | 5 秒 |

**我们的现状**：`stagnation_repeat_limit=5`（连续 5 条相同）。
**实测触发率：0**——本轮 3 个 Run 的最长连续重复只有 1/2/2。

我又回测了滑动窗口方案（15 条窗口内同前缀 ≥5 次）：SOLVED 35% / UNSOLVED 41%，**区分度 1.2 倍，不够格上线**（当年校准 `stagnation_command_limit=40` 时是 2.4 倍）。**不改。**

⇒ 文献的 3 次阈值是**通用的**，不是为 CTF 校准的。在我们的数据上，更激进只会误伤成功 Run。

## 三、缺口题的实际状态（实测）

### 3.1 36 道待跑题的真实构成

队列 72 SOLVED / 35 PENDING / 22 UNSOLVED / 1 RUNNING。

正确口径下真实缺口只有 **6 道**：
```
Phuck2 / [极客大挑战 2018]Comment / [极客大挑战 2019]Upload
[SUCTF2018]Unfinish / [De1CTF 2019]Giftbox / [STARCTF2019]996game
```

### 3.2 这 6 道的方法论提示实测

```
[STARCTF2019]996game         hints=0  id=none  brief=[]
Phuck2                       hints=0  id=none  brief=[]
[极客大挑战 2019]Upload       hints=0  id=none  brief=[]
[SUCTF2018]Unfinish          hints=0  id=none  brief=[]
[De1CTF 2019]Giftbox         hints=0  id=none  brief=[]
[极客大挑战 2018]Comment      hints=0  id=none  brief=[]
```

**全部为空。** 两条通道都拿不到任何信息。

### 3.3 996game 已跑完，质量分析

| 指标 | 值 |
|---|---|
| 命令数 | 291 |
| fact 数 | 5 |
| dead_end | 3 |
| 耗时 | 26 分钟 |
| 结果 | **UNSOLVED** |

它的 5 条 fact 质量很高：

```
1. socket.io 1.7.4 协议确认（/socket.io/socket.io.js，<len>:42[...] 分帧）
2. init-world 协议确认：ws://target/socket.io/?EIO=3&transport=websocket，发 40 再发 42["init-world",...]
3. eval sink 定位在 js/server/GameServer.js:281，loadPlayer 的 findOne 错误回调，
   且仅在 err && !doc 时求值
4. NEED_INPUT：init-world handler 未暴露 + 自称 TLS EOF
```

**它已经做到了**：定位漏洞点、确认利用条件、写出完整协议。
**它缺的是**：`init-world` 的具体 payload 结构——而这一步它放弃了，理由是（错误的）网络不通。

⇒ **这不是能力问题，是「在最后一跳前误判环境」**。

## 四、解决方案

按「可验证性 × 收益 / 成本」排序。**每条都给了判据，不达标就回滚。**

### P0：补齐执行层的知识注入通道（对应 CRAKEN 21% vs 17%）

**问题**：6 道缺口题两条通道全空。

**做法**：不引入外部语料（已证明无效），而是把**本库自己已解 Run 的轨迹**转成执行层提示。数据已有：
- 380 个 Run 有真实命令流
- 已解常规题 19 道，平均 4–17 条 fact

**具体**：`brief_lines()` 在无 curated hints 时，回退到「同赛题/同 tag 的最近已解 Run 的前 5 条 fact + 前 3 条命令」。
CRAKEN 证明执行层注入优于规划层，且我们必须用自己的实测数据（外部语料检索轴对不上）。

**判据**：6 道缺口题中，注入后至少 1 道解出（或 fact 数中位数提升 ≥50%）。不达标即回滚。

**风险**：可能引入无关提示造成干扰（CRAKEN：混合语料 15.5% < 单源 21%）。所以必须**单一来源**——同赛题优先，没有就留空。

### P1：NEED_INPUT 的正确处理（成本最低）

**问题**：`996game` 因误判「网络不通」放弃。

**做法**：Worker 输出 `NEED_INPUT` 时，Coordinator 注入一条**实测事实**：「本容器 egress 实测 200（google/github/api.github.com），网络可用；你的结论若不基于实测请重试」。

**判据**：下一个出现 `NEED_INPUT` 的 Run，检查后续是否继续尝试而非收敛。这是**行为指标**，不依赖解题成功率，样本需求小得多。

**注**：我只观察到 1 例 Need_Input + 误判。所以这条的优先级取决于是否复现——先加一个计数器，等第 2 例出现再动手。

### P2：把「漏洞点已定位」和「利用失败」区分开

**问题**：996game 定位到 `GameServer.js:281` 的 eval，却因为不知道 payload 结构而停在最后一跳。

**观察**：这正是文献说的「复合概率链」——它前面全对了，最后一跳断。Adaline 给的解法是**检查点 + 可恢复状态**，Anthropic 的做法是「把长任务切成有验证标准的里程碑」。

**做法**：当 Worker 已经产出「sink 位置 + 触发条件 + 协议」三类 fact 时，Coordinator 应当把收敛目标从「解出」改成「验证这条链的最后一环」，而不是整体放弃。

**判据**：这类 Run（有 ≥3 条高质量 fact 且含 sink 定位）的二次尝试成功率。

**这条我做不了验证**——需要多次 Run 样本，当前没有。列为待观察。

### P3：可信正样本仍是最大空白

**CTF-Dojo 的核心结论**：486 条**执行验证过的**成功轨迹，就能把 32B 模型从 20.3% 提到 31.9%，且收益随数据量近似线性。

**我们的现状**：`fresh_reproduction_verified` 全库仍是 **0**。

⇒ **没有可信正样本，就没有可训练的数据，也就走不到文献里最有效的那条路。**

这是毕业设计层面真正的瓶颈，比任何 runtime 调参都重要。补齐它的优先级应高于 P1/P2。

## 五、我不打算做的事，以及为什么

| 不做 | 原因 |
|---|---|
| 扩技法语料 | 已实做并回滚。计数 9→398，四道题候选列表一字不差 |
| 加快滑窗去重 | 已回测。SOLVED 35% / UNSOLVED 41%，区分度 1.2 倍不够 |
| 在 25 个样本上调参 | 噪声上做梯度下降 |
| 宣称「超越 SOTA」 | 题目分布不同、有配方注入、样本 n=25，三个理由都不支持 |

## 六、参考资料

| 文献 | 出处 | 本文引用点 |
|---|---|---|
| NYU CTF Bench | NeurIPS'24 Datasets & Benchmarks | 基准与水位 |
| EnIGMA | arXiv:2409.16165 | IAT 设计、13.5% 水位 |
| CRAKEN | arXiv:2505.17107 | 执行层 vs 规划层注入、语料形态 |
| CTF-Dojo | ICML'26 / arXiv:2508.18370 | writeup hint +7.4%、486 轨迹、随机化 +24.9% |
| PentestEval | arXiv:2512.14233 | 端到端 31%、分阶段诊断 |
| HORIZON / Context Rot | arXiv（Wang et al.） | 长程衰减、上下文饱和 |
| awesome-agent-failures | Vectara | 循环检测阈值、四层防御 |

开源实现可参考：
- https://github.com/GreyDGL/PentestGPT
- https://github.com/NYU-LLM-CTF/nyu-ctf-bench
- https://github.com/SWE-agent/SWE-agent（EnIGMA 基于此）
- https://github.com/nbshenxm/pentest-agent（PentestAgent）

---

## 七、P0 方案的可行性实测（写完就去验了，结果推翻一半）

我在 §4 提的 P0 是「用同赛题已解 Run 的轨迹做执行层注入」。写完后立刻查数据验证：

| 缺口题 | 同赛题 tag | 同赛已解 | 同赛未解 |
|---|---|---|---|
| Phuck2 | 无 tag | — | — |
| [极客大挑战 2018]Comment | 极客大挑战 2018 | **0** | 0 |
| [极客大挑战 2019]Upload | 极客大挑战 2019 | **0** | 0 |
| [SUCTF2018]Unfinish | SUCTF2018 | **0** | 0 |
| [De1CTF 2019]Giftbox | De1CTF 2019 | 1 | 4 |
| [STARCTF2019]996game | STARCTF2019 | **0** | 1 |

**6 道里 5 道的同赛已解数是 0。** 唯一例外是 Giftbox（同赛 1 道已解）。

⇒ **P0 在原始形态下不成立。** 想凑数据就只能跨赛题混合，而 CRAKEN 的实测明确说混合语料会变差（15.5% vs 单源 21%）。

### P0 修正为 P0'

只在**同赛题有已解**时注入，否则留空。当前能覆盖的面：1/6。

这不值得作为主线。**但它揭示了一个更本质的事实：**

> 这 6 道题之所以解不出，是因为它们在本库里是**孤例**——没有同类先例可供类比，也没有外部 writeup 可查（题名是自造的 `996game`、`Phuck2`，不是 CVE）。

这正是文献里 CTF-Dojo 用「公开 writeup」补的东西（+7.4%），而我们的题名恰恰检索不到 writeup。

### 因此真正的主线应该是 P3（可信正样本），不是 P0

CTF-Dojo 的 486 条轨迹之所以有效，前提是**每条都执行验证过**。我们的 `fresh_reproduction_verified` 是 **0**。

没有验证过的正样本 ⇒ 没有可训练数据 ⇒ 走不到文献最有效的那条路（20.3% → 31.9%）。
而且没有它，「这道题解出来了」这个事实本身也不可信。

**结论：把补齐 `fresh_reproduction_verified` 提到 P0，runtime 调参全部降级。**

## 八、修正后的优先级

| 优先级 | 事项 | 依据 | 判据 |
|---|---|---|---|
| **P0** | 补齐 `fresh_reproduction_verified`（可信正样本） | CTF-Dojo：486 条验证轨迹即 20.3%→31.9% | 至少 20 道题有可复现验证记录 |
| P1 | `NEED_INPUT` 误判计数器 | 现仅 1 例（996game），不足以动手 | 累计 ≥3 例再改 |
| P2 | 有 sink 定位的 Run 二次尝试 | 996game 断在最后一跳 | 需多次 Run 样本 |
| P0'（降级） | 同赛题轨迹注入 | 实测仅覆盖 1/6 | 仅 Giftbox 可验 |
| 不做 | 扩语料 / 滑窗去重 / 小样本调参 | 均已实测证否 | — |

---

## 九、★★★ 找到了 `fresh_reproduction_verified` 恒为 0 的确切原因

上节说「补齐可信正样本是 P0」，随后就去查了它为什么一直是 0。结论是**一个具体的工程错配**，不是能力问题。

### 9.1 因果链（已完整验证）

```
Run 解出 → 平台 accepted
  → arena_solve.py:945  await _verify_reproduction(...)
    → fresh_reproduction.py:104  await runner_client.create_job(...)
      → runner_client.py:65  _require_enabled()
        → if not settings.legacy_kali_runner:  raise RunnerRetiredError
          → arena_solve.py:1022  catch Exception → {"status":"error","reason":"RunnerRetiredError"}
            → fresh_reproduction_verified 永远 = False
```

### 9.2 数据佐证

```
ctf_agent.run_events where event_type='arena.fresh_reproduction'
  status='error'  reason='RunnerRetiredError'   →  37 条
  （没有任何 success / timeout / executed）
```

```
solve_runs 总数 742，fresh_reproduction_verified=1 的：0
```

**37/37 全部是同一个异常，一条都没进入过真正的重放。**

### 9.3 为什么

`fresh_reproduction.py` 的执行器仍然调用 `runner_client`——那是**已下线的 legacy Kali Runner**。生产环境用的是 Muteki 容器化 Worker（`solver_mode=muteki`），`legacy_kali_runner=False`，所以 `_require_enabled()` 在第一步就抛异常。

**复现验证的代码挂在一条被废弃的执行通道上，因此从未运行过一次。**

这不是「忘了开开关」，是**两个时代的架构并存，其中一条已经断电**。

### 9.4 这解释了为什么它比任何 runtime 调参都重要

- CTF-Dojo 的 486 条轨迹之所以能把 32B 从 20.3% 提到 31.9%，前提是**每条都执行验证过**
- 我们没有可信正样本 ⇒ 没有训练数据 ⇒ 走不到文献最有效的那条路
- 更糟的是：**「解出」这件事本身在我们的记录里是不可复现的**，因为验证通道断电了

### 9.5 修法（明确、有限、可验证）

把 `FreshReproductionExecutor` 的执行层从 `runner_client` 换到 Muteki 的容器执行通道（`container_exec`，已在 `muteki/` 里用于正常求解）。

**判据**：下一道解出的题，`arena.fresh_reproduction` 事件出现 `status != error`。这是**二值判据**，不需要统计样本。

**风险**：`REPRODUCTION_BUDGET_SECONDS=180`（arena_solve.py:969）可能对复杂题过短——但当前连 180 秒都没跑满就抛异常了，所以先解决「跑不起来」，再谈「跑不完」。

### 9.6 顺带确认的一件事

`_verify_reproduction` 的注释写着：

> 「environment slot is still held, so an unbounded replay would block every later challenge」

也就是说，180 秒的预算是为了**不占住唯一的环境槽**。这意味着修好之后，复杂题仍可能超时——但那会是 **timeout**（可度量、可归因），而不是 **error**（不可见的静默失败）。先让失败变成可见的。

---

## 十、实时验证：Giftbox 解出，同时复现了根因

02:22，`[De1CTF 2019]Giftbox` 被平台 **accepted**。

| 指标 | 值 |
|---|---|
| Run | `9847adf6` |
| 命令数 | **54** |
| fact 数 | **9** |
| 耗时 | **4 分钟** |

它是 6 道缺口题里**唯一有同赛题已解**（同 tag 已解 1 道）的那一道。

同时，它作为刚解出的题，立即触发了复现验证——事件内容：

```json
{"reason": "RunnerRetiredError", "status": "error", "verified": false, "budget_seconds": 180}
```

**这就在一个实时案例上复现了 §9 的根因**：解出 → 平台 accepted → 复现验证 → 立即抛 `RunnerRetiredError` → `fresh_reproduction_verified` 保持 False。

§9 的因果链原本是从 37 条历史事件反推的，现在有了第 38 条实时观测，且是在我自己盯着的 Run 上。

### 缺口收敛

6 → **5** 道：
```
Phuck2 / [极客大挑战 2018]Comment / [极客大挑战 2019]Upload
[SUCTF2018]Unfinish / [STARCTF2019]996game
```

### 但不要说 Giftbox 证明了 P0'

它是唯一有同赛已解的那道，而我没有实际部署同赛轨迹注入——这次解出是模型自己做到的（54 条命令、9 条 fact，4 分钟，属于干净利落的解）。

**能说的只有：它解出了，且 n=1。** 与旁边 5 道（同赛已解=0）的对比，恰好符合「孤例题更难」这一预期，但 1 vs 5 的样本不足以称为证据。

## 十一、当前状态（02:30）

| 项 | 值 |
|---|---|
| 队列 | 73 SOLVED / 34 PENDING / 22 UNSOLVED / 1 RUNNING |
| 常规题 | 20 / 26 = **77%**（按尝试过口径） |
| 缺口 | 5 道 |
| 回归 | 2473 passed |
| 看门狗 | `--once` 每 4 分钟自检 |

### 最重要的产出不是这 1 道题

是 §9 那条因果链：**`fresh_reproduction_verified` 恒为 0 不是数据问题、不是模型问题，是复现验证的执行层还挂在已下线的 legacy Kali Runner 上。**

这条一旦修好，`fresh_reproduction_verified` 才开始有值；有了它，CTF-Dojo 式的「486 条验证轨迹 → 20.3% 提到 31.9%」那条路才成立。而在修好之前，**任何训练、任何权重微调都缺乏可信的正样本基础**。
