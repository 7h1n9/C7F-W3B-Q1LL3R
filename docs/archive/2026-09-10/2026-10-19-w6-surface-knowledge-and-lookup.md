# W-6：题面声明 / 参考知识库 / 公开 PoC 联查

日期：2026-10-19
验证 Run：`9e095f0c-91d8-46c4-bc69-e059c247755b`（`sql-injection-golden`，单臂 `on`）

## 0. 结论

三项改造全部落地，端到端跑通，并在这个过程中挖出一个**比三项改造本身更致命的缺陷**：

| # | 内容 | 状态 | 判据 |
|---|---|---|---|
| W-6 | 题面声明注入 Reason / A 路 Worker / 工具精修三层 | ✅ | 意图从「猜参数」变成直接打 `department` |
| K-1 | Des-CTF-Knowledge 入库 656 条 | ✅ | 语料 31 → 687 条，loader 全量通过 |
| K-2 | `payload_template` 进入先验渲染 | ✅ | 此前该字段存了但从未被任何 formatter 读取 |
| L-1 | 公开 CVE / PoC 联查工具 | ✅ | 工具契约 + 12 项测试；真实查询命中 CVE-2021-44228 |
| **F-1** | **flag 从未写入 `flag_candidates`** | ✅ **修复** | 修复前解出也判 `INCOMPLETE`；修复后 `final_result: SUCCESS` |

最终实测：**390 秒解出，`verified_flag_rank: 1`，`solved_within_budget: True`**。

这是这条链路上第一次出现「Run 真解出 ∧ 评测真认账」。

---

## 1. W-6：题面声明的三条通道

### 1.1 问题

`challenges.metadata_json` 里的 `initial_knowledge` / `attack_surface` 只被
`methodology_hints.hint_text()` 渲染进 workspace 的 `AGENTS.md`。那个文件只有 CLI
Worker 读，A 路（`chat_completions`）与 Coordinator Reason 都不读。

Run `e3391674` 的 `muteki.intent_proposed` 是直接证据：

> technique 写对了（"boolean/LIKE injection oracle ... tautology/contradiction pairs"），
> 参数写成了 `serial` / `warranty_id` / `product_id`。

### 1.2 修法

声明走**独立通道**，不混进 `knowledge_prior`：

| 通道 | 位置 | 形态 |
|---|---|---|
| Reason | `CoordinatorReasonModel.challenge_brief` | `## Challenge declared surface` 段落，置于 prior **之前** |
| A 路 Worker | `OpenAICompatibleWorker.challenge_brief` | prompt JSON 的 `challenge_declared_surface` 字段，置于 graph 摘要**之前** |
| 工具精修 | `MutekiRuntime._openai_worker_action` | 同上，因为这一层才真正写 `arguments` 里的参数名 |

不混进 prior 的理由写在代码里：prior 是**待验证假设**，渲染在 “Unverified” 标题下；
声明是题面给定事实。把两者合在一起，等于让模型怀疑唯一该当成给定的输入。

传递路径：`MutekiRuntime._brief_environment()` → `EngineProfile.environment`
（`MUTEKI_CHALLENGE_BRIEF`，JSON 编码）→ `_parse_challenge_brief()` →
worker 构造参数。JSON 而不是换行拼接：某条声明自己可能含分隔符。

`_declared_surface()` 用 `getattr` 兜底，因为集成测试用 `MutekiRuntime.__new__`
绕过 `__init__`（不跑 DB 绑定）。

### 1.3 效果

同一用例、同一模型，修复前后意图对比：

| Run | 意图里的参数 | 结果 |
|---|---|---|
| `e3391674` | `serial` / `warranty_id` / `product_id` | 483s，8 次脚本，0 flag |
| `9e095f0c` | **`department`**，直接点名 `POST /api/warranty/check` | **390s，SOLVED** |

`9e095f0c` 的脚本序列也印证了它在按标尺走：`table_exists` → `db_name` →
`col_exists_A` → `ncols: 3 req: 3`。

---

## 2. K-1：参考语料入库

来源：`D:\desktop\毕业设计\参考项目\Des-CTF-Knowledge`（1209 个 md）。
转换器：`scripts/import_des_ctf_knowledge.py` → `data/knowledge/des-ctf.jsonl`。

### 2.1 为什么是「切片」而不是「复制」

检索层用 BM25 排序 `KnowledgeItem` 文档。一篇 200KB 的文章是**一个**文档，对任何具体
意图都匹配不中，必然输给一条 300 字的条目。可匹配的最小单位是**小节**。

### 2.2 口径

| 项 | 规则 |
|---|---|
| 纳入 | 13 个技术文档（SQL / 命令执行 / 文件上传 / 文件包含 / SSRF / PHP 反序列化 / SSTI / 代码审计 / JWT / 压缩包 / 图片隐写 / 音频隐写 / Payload 速查）+ `WP汇总/` + `工具使用/` |
| 排除 | 许可证 / CHANGELOG / README / PROGRESS / `.idx.md`（面向机器的行号索引）/ `python-note` 类语言入门笔记 |
| 切片 | `##` 与 `###`；**围栏感知**（SQL.md 里有以 `#` 开头的 SQL 注释行，朴素扫描会在 payload 中间切断） |
| `kind` | 有代码块 → `poc`，否则 `technique`；**不用 `case`/`negative`**，因为这两类要求 `known_failures`，而转换器无法诚实断言一条没人跑过的路线 |
| `trust_level` | `unverified`（外部参考，未在本项目靶场复核） |
| `provenance.source_type` | `reference-corpus` |
| 跳过 | 正文 < 60 字且无代码块；导航型标题（目录/参考/简介）；重复 id |

结果：**656 条**（490 `poc` / 166 `technique`），语料总量 31 → **687**。

### 2.3 召回实测

| 查询 | Top-1 |
|---|---|
| `boolean blind SQL injection ...` | `sqli-boolean-blind`（原 curated，55.5） |
| `file upload bypass .htaccess webshell` | `des-ctf-...` 文件上传漏洞 — 后端检测 Content-Type |
| `ssrf gopher redis fastcgi` | `des-ctf-...` SSRF漏洞 — Gopher 攻击 FastCGI |
| `JWT none algorithm bypass` | `jwt-alg-none`（原 curated，27.3） |

关键点：原有 31 条 curated 在自己最匹配的领域**仍然排第一**，说明新语料没有把 IDf
稀释到失效；同时补上了此前完全召不回的 SSTI / SSRF / 文件上传。

---

## 3. K-2：`payload_template` 从未被渲染

`KnowledgeItem.payload_template` 在 schema 里存在，导入时也写了，但
`format_prior()` 与 `format_worker_prior()` 都只读 steps / verification / failures。

后果：一条 PoC 以散文形式到达模型，唯一能逐字复制的字段不可见。

修法：新增 `_payload_block()`，**保留换行**（不同于 `_compact` 的压平），按行边界截断
（prior 400 字符 / worker 300 字符 6 行）。截断落在行中间会产生半个 payload。

---

## 4. L-1：公开 CVE / PoC 联查

工具：`lookup_public_poc`（A 路 `WORKER_TOOLS` 第 6 个），实现
`app/solver/muteki/worker/public_poc.py`。

### 4.1 边界（这是设计重点）

| 约束 | 实现 |
|---|---|
| 不下载、不执行 PoC | 只返回结构化摘要 |
| 只查两个域 | `ALLOWED_HOSTS` **被 `_get()` 真正校验**，不是注释 |
| 超时 | 单次 9s，失败返回 `{"success": false}` 而不抛 |
| 每 Worker 预算 | 4 次 |
| 缓存 | 进程内 LRU 64，同关键词不重复打 API |
| 语义 | 命中的 CVE 是**假设**；写入 SharedGraph 时 `verified=False`、confidence 0.3 |

`verified=False` 这一条很关键：否则一个 Worker 的查询会变成另一个 Worker 的「证据」，
而 `submit_flag` 的观测校验就形同虚设。

### 4.2 实测

| 查询 | 结果 |
|---|---|
| `apache struts 2.5.20` | CVE-2019-0230，CVSS 9.8 CRITICAL，摘要正确 |
| `CVE-2021-44228` | 精确命中 + `fullhunt/log4j-scan` 等 3 个 PoC 仓库 |
| GitHub 403（限流） | 降级为「无仓库」，仍保留 NVD 记录 |

---

## 5. F-1：flag 台账断链（本次最严重的发现）

### 5.1 现象

Run `9e095f0c` 解出后：

```
solve_runs.status            = COMPLETED_SOLVED
run_events.muteki.flag_found = flag{93ffaeedc961d108d17fddd66b90487d}   (verified=true)
evaluation_report.final_result = INCOMPLETE
evaluation_report.evaluator.evidence = {"verified_flag_candidates": 0}
```

flag 值与靶场 `WARRANTY_FLAG` **逐字一致**，仍然判 0 分。

### 5.2 根因

```python
# app/benchmark/evaluation.py
final_result = "SUCCESS" if verified_flag or finding_created else ...
verified_flag = any(item["verified"] is True and item["review_state"] == "VALID"
                    for item in flag_candidates)   # ← 只读 flag_candidates
```

而 Muteki 路径把 flag 只写成 `run_events.muteki.flag_found`，
**从未写 `flag_candidates`**。查库确认：该表最后一条记录是 2026-10-15，
全部来自旧的多 agent 路径。

即：**10-16 之后所有由 Muteki 解出的题，评测层一律看不到。**
CAP-03 配对矩阵的 `pass@1` 因此会被结构性地压成 0 —— 这比之前记录的三个阻塞
（靶场单一 / 墙钟 / 两臂都不解）更致命，因为它让「能解」也无法体现。

### 5.3 修法

`FlagService.record_runtime_flag()`：在 `run_supervisor._run_muteki` 收尾处调用，
那里同时拿得到 ORM `Challenge`（含声明 banner）与 `SolveRun`。

判定口径遵守「不信任产出方」：

```python
declared = _matches_declared(text, challenge.flag_pattern)      # 严格 fullmatch
valid    = matches_flag_format(text, challenge.flag_pattern)    # 含 generic 兜底
accepted = valid and declared
review_state = "VALID" if accepted else "OPEN"
```

`matches_flag_format` 会 fallback 到通用 `banner{body}` 形状（这是为了让 banner 配错时
真实 flag 仍可表达）。这个 fallback **不能单独产生 solve** —— 否则「banner 错、flag 长得像」
会被自动提交。所以两项都满足才 VALID，否则 OPEN 等人工判。

`suspected_flags` 也一并落表（同样 OPEN），这样人工复核时不用去翻 run_events。

### 5.4 修复验证

补录工具 `scripts/backfill_runtime_flags.py`（同时可修历史 Run）：

```
$ python scripts/backfill_runtime_flags.py --run-id 9e095f0c-...
9e095f0c-...  COMPLETED_SOLVED  flag{93ffaeedc961d108d17fddd66b90487d}
{"runs_scanned": 1, "candidates_missing": 1, "now_verified": 1}
```

DB 行：`verified=1, review_state=VALID, pattern_matched=1, first_seen_source_type=MUTEKI_RUNTIME`

重跑评测：

```
final_result: SUCCESS
verified_flag_rank: 1
solved_within_budget: True
evaluator.evidence: {"verified_flag_candidates": 1, "finding_created": false}
```

---

## 6. 端到端对照

| Run | 改动 | 状态 | 墙钟 | 真实工具步 | flag | 评测 |
|---|---|---|---|---|---|---|
| `e3391674` | python_run | UNSOLVED | 483s | 27（8 次脚本） | 无 | INCOMPLETE |
| `9e095f0c` | + W-6 + K-1 + L-1 | **SOLVED** | **390s** | 27（14 次脚本） | `flag{93ff...}` | **SUCCESS** |

注意 `summary.json` 里的 `average_tool_calls = 3` 仍不是工具步数：A 路只往
`tool_calls` 表写 `muteki_native_worker`（每个 Worker 一次）。真实 27 步在
`run_events.muteki.worker_step`。这个口径差异是既有的，未改（改了会与历史产物不可比）。

---

## 7. 未做

| 项 | 说明 |
|---|---|
| `articles/` 1156 篇 WP | 转换器已支持 `--include-articles`，但会产出上万条、稀释 top_k。要不要收需要先定分层召回策略 |
| 历史 Run 批量补录 | 工具支持 `--since`，尚未对 10-16 以来全部 Run 执行 |
| CVE 联查的「产品指纹」自动识别 | 当前靠模型自己判断何时调用；未做从响应头/HTML 自动提取产品版本 |
| CAP-03 配对矩阵 | 阻塞 C（两臂都不解）与本次的 F-1 都已修复，现在跑才有意义 |
