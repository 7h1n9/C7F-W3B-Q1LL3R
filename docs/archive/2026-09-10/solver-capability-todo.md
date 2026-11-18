# 解题能力提升待办清单（Solver Capability TODO）

> 项目：`C7F-W3B-Q1LL3R`（多引擎 Web 安全自动推理系统）
> 编制时间：2026-10-06
> 编制依据：`D:\desktop\codex` 下的任务状态文件 + 当前工作区源码实证核对
> 范围声明：**本清单只收敛"解题能力"相关的工程与实验动作**。协调器容器化、UI 视觉、部署形态等议题不在此列。

---

## 0. 三条硬约束（先读）

1. **未验证的提升不能写成成果。** 任何"成功率提升"必须由 evaluator-backed 的配对 A/B 证明；
   配对样本 < 10 时结论只能是 `descriptive_only`，`success_delta` 必须为 `null`。
   出处：`codex\c7f-knowledge-augmentation-strategy\CONTEXT.md` §3/§4、`codex\c7f-experiment-8ddbdb34-analysis\CONTEXT.md` §6、
   桌面 `C7F-W3B-Q1LL3R-知识增强与评测落地方案.md` §15 第 7 条。
2. **知识永远是 PRIOR，不能绕过 VerifiedFact gate。** 只有当前 Run 的真实工具输出与证据可以晋升为已验证事实。
3. **不要为了跑实验而伪造数据。** 历史 descriptive-only 的 solve 不能当 evaluator 成功用。

---

## 1. 总览

| ID | 优先级 | 事项 | 主要出处 | 实证现状 |
|---|---|---|---|---|
| CAP-01 | P0 | 给挑战绑定 evaluator case（`benchmark_case_id`） | 8ddbdb34 分析 §5 | **已做**：配对新增强制门禁 `SOURCE_CHALLENGE_NOT_EVALUATABLE`(409)；题库侧仍待补绑定 |
| CAP-02 | P0 | 持久化召回条目 ID 与分数（现在只落 stats） | 8ddbdb34 分析 §5 | **已做**：`REASON_MODEL_SELECTED` 现落 `knowledge_query`/`knowledge_top_k`/`knowledge_items`(id,kind,score) |
| CAP-03 | P0 | 配对样本量 + 统计口径落地（≥10、McNemar） | 知识方案 §10/§11 | **部分**：audit API 已强制 `descriptive_only`，但批量 runner 未跑 |
| CAP-04 | P0 | 知识条目 `published_at` 去伪（20/25 条同一天） | 知识方案 §4.2 | **半做**：排除开关 + 审计告警已实现；**真实日期仍待人工回填**（实测仍 20/25 条占位） |
| CAP-10 | P1 | 模型重试加 jitter | coordinator 任务 §5 第 3 条 | **已做**：`backoff_delay(jitter=)`，引擎默认 0.5，`Retry-After` 为不可下穿地板 |
| CAP-11 | P1 | 跨题案例库（现仅同题历史） | 知识方案 §2.2 第 7 条 | **未做**：`challenge_lessons` 只查同 `challenge_id` |
| CAP-12 | P1 | 知识语料扩容 + 补齐 `case`/`negative` 类型 | 知识方案 §6.2/§12 阶段1 | **未做**：25 条、只有 technique/poc |
| CAP-13 | P1 | 错误先验导致的额外失败率指标 | 知识方案 §11 第 9 条 | **未做** |
| CAP-14 | P1 | 无效尝试 / dead-end 重复率指标 | 知识方案 §11 第 6 条 | **未做** |
| CAP-15 | P1 | LearnedSkillCandidate 审核后发布与索引链路 | 知识方案 §2.2 第 8 条 | **未做**：有 review，无 publish/index |
| CAP-20 | P2 | Worker 事实 → ToolCall/EvidenceLedger/VerifiedFact 投影完整性 | 知识方案 §2.2 第 3 条 | 待逐环节核对 |
| CAP-30 | P2 | Reason/Coordinator 动态扩容与策略深度 | 知识方案 §2.2 第 4 条 | 本次不做（协调器议题） |
| CAP-31 | P2 | 批量实验矩阵 runner + 靶场重置（含 CTF-Forge 式自动容器化） | 知识方案 §8.2 + CTF-Dojo[3] | 待评估 |
| CAP-32 | P1 | 执行验证轨迹闭环（蒸馏写端错位 + 读端零调用点） | CTF-Dojo[3] §2/§3 | **已做**：三段全通（`templates/` 目录 + 只读开图 + 正负两类蒸馏 + 召回接线），58 项测试绿 |
| CAP-33 | P1 | 评测口径补 Pass@k / 首解率 / 预算内成功 | CTF-Dojo[3] | **已做**：`attempt`/`turns_used`/`turn_budget`/`solved_within_budget`/`verified_flag_rank` + `aggregate_pass_at_k()` |
| CAP-34 | P1 | 运行时随机化（端口 / flag / 路径防过拟合） | CTF-Dojo[3] runtime augmentation | **未做**：case 的 target 为硬编码常量 |

---

## 2. P0 —— 先修"能力无法被度量"

### CAP-01　给挑战绑定 evaluator case

- **出处**：`codex\c7f-experiment-8ddbdb34-analysis\CONTEXT.md` §5 ——「same challenge d1e9e43e … `challenge metadata_json.benchmark_case_id` is NULL -> no evaluator case binding」，导致两臂 `evaluation_report=null / success=null / flag_candidates` 为空。
- **现状**：`benchmark_case_id` 只在实验入口写入（`backend/app/benchmark/experiment.py:1331`），
  读取侧分散在 `app/services/security_fact_reducer.py:29`、`app/services/tool_result_fact_reducer.py:101,246`、
  `app/services/approved_action_compiler.py:173`（后两处按 `"sql-injection-golden"` 特判）；题库侧无强制字段。
- **动作**：
  1. 在挑战创建/导入路径上把 `benchmark_case_id` 设为必填或显式 `null` 并告警；
  2. 给 A/B 配对入口加前置校验：源 Run 的 challenge 未绑定 case 时**拒绝创建对照组**（或标记 `evidence_unavailable`），避免又跑出一组不可用数据。
- **验收**：新建一个配对实验时，若 challenge 无 `benchmark_case_id`，接口返回明确错误码而不是静默产出 `descriptive_only`。

### CAP-02　持久化召回明细（条目 ID + 分数 + top_k）

- **出处**：8ddbdb34 分析 §5 ——「Exact 3 item IDs not persisted; preview reproduction with reconstructed query suggests generic/off-target items (file upload, SSRF) rather than SSTI; labeled indicative only.」
- **现状**：`RetrievalRecord` 存在于内存（`backend/app/knowledge/retriever.py:298 _save_log`），
  但实验复查时无法从 DB 还原。本轮已新增 `runtime.worker.knowledge_prior` 事件
  （`muteki_runtime.py:714`，含 `item_ids` / `chars`）**只覆盖 Worker 路径**；Reason 路径的召回仍未落库。
- **动作**：
  1. Reason 路径补发同构事件（或统一为 `knowledge.retrieved`），字段含 `run_id / intent_id / phase / query / item_ids / scores / top_k / mode`；
  2. 前端知识审计页展示每次召回的条目与分数。
- **验收**：任取一个历史 Run，能仅凭 DB 复述"这一轮召回了哪几条、各自多少分"，无需重建 query。

### CAP-03　批量实验与统计口径

- **出处**：桌面方案 §10/§11、`c7f-knowledge-augmentation-strategy\CONTEXT.md` §8 Risks。
- **现状**：控制组配对（`POST /api/v1/runs/{run_id}/control`）、A/B 聚合与
  `descriptive_only` 门禁已实现；**批量 runner 与 2-3 次重复未执行**。
- **动作**：按方案 §8.2 实现 `case × arm × repetition` 批量 runner（含靶场重置、暂停恢复、manifest 一致性跳过）；
  汇总层实现配对差值 + McNemar（二分类）/ bootstrap CI（连续）。
- **验收**：同一 manifest 重跑得到结构一致的输出目录；汇总结果可由原始日志重算。

### CAP-04　知识条目 `published_at` 去伪

- **实证（本次核对）**：`data/knowledge/*.jsonl` 共 **25 条**（injection 10 / request-and-files 8 / auth-deserialization 7），
  但 `published_at` 分布为 **`2020-12-03` × 20**、`2024-02-29` × 3、`2015-05-01` × 1、`2011-09-01` × 1。
- **问题**：桌面方案 §4.3 要求「逐题快照 `knowledge_item.published_at < test_challenge.start_at - buffer`」，
  但 20/25 条共用一个占位日期 —— **时间切分当前无法真正起效**，泛化实验会被这一点直接否掉。
- **动作**：为每条条目回填真实来源与发布时间（含 `source_type`/`source_ref`），
  无法确认原始来源的条目标记为 `published_at: null` 并在检索时按配置排除。
- **验收**：任何一条被用于时间留出实验的条目，都能独立溯源到原始出处；`2020-12-03` 不再是批量占位值。

---

## 3. P1 —— 有明确代码落点的能力增强

### CAP-10　模型重试加 jitter

- **出处**：`codex\c7f-coordinator-reason-outage\CONTEXT.md` §5 第 3 条 ——
  「考虑给 coordinator reason 配置独立于 worker 的渠道/并发额度，**或对 5xx 做带 jitter 的多次重试**」。
- **现状**：`backend/app/engines/retry.py::backoff_delay` 为**确定性** `min(base * 2**attempt, cap)`，无 jitter；
  `openai_compatible.chat()` 已接上该退避（`:176,191,202,368,384`）。事故复盘明确指出「4 run 并发 + 长请求」是最脆弱场景。
- **动作**：给 `backoff_delay` 增加可选 jitter（如 `full jitter: random.uniform(0, delay)` 或 `delay * (0.5 + random())`），
  默认开启、可通过参数关闭以保持既有测试的确定性断言。
- **验收**：新增聚焦测试覆盖 jitter 上下界；既有 4 个 `test_openai_compatible_chat_retry.py` 用例仍通过（注入固定随机源）。

### CAP-11　跨题案例库

- **出处**：桌面方案 §2.2 第 7 条 ——「`challenge_lessons` 只保存同一 challenge 的历史工具策略，不是跨题案例库」。
- **现状**：`backend/app/services/challenge_lessons.py:27` 查询条件为
  `SolveRun.challenge_id == challenge_id AND status == "COMPLETED_SOLVED"`，只取同题最多 20 个 run，
  产出 `effective_tool_order / verified_attack_surface / common_blockers / argument_shapes / avoid_repeated_behaviors`。
- **动作**：
  1. 新增按**技术栈/题型**聚合的 lesson 视图（如 `target_stack` + 漏洞类别），复用同一份统计口径；
  2. 通过知识通道（`kind=case`）下发，而不是塞进 context builder 的固定字段；
  3. 明确它与 CURATED 语料的边界：历史 lesson 是**本项目自产先验**，可信级别低于 `reviewed` 语料。
- **验收**：新题（无同题历史）也能拿到按题型聚合的先验；先验里能区分"自产"与"人工整理"。

### CAP-12　知识语料扩容 + 补齐类型

- **出处**：桌面方案 §6.2（四种 kind：technique/case/poc/negative）、§12 阶段 1（20-40 条）。
- **现状**：25 条，`kind` 只有 `technique`(16) 与 `poc`(9)；**`case` 与 `negative` 均为 0 条**。
- **动作**：
  1. 补 `kind=negative`（已确认不可行的路线与触发条件）——这是"降低无效尝试"最直接的手段，当前完全缺失；
  2. 补 `kind=case`（跨题的成功/失败路线摘要，与 CAP-11 联动）；
  3. 扩到方案建议的 20-40 条区间并覆盖 ≥3 类题型。
- **验收**：四种 kind 均有条目；每条带 `preconditions` / `verification_method` / `known_failures`。

### CAP-13　"错误先验导致的额外失败率"

- **出处**：桌面方案 §11 第 9 条（过程指标）。
- **现状**：**未做**。这是唯一能证伪知识增强的指标（先验误导导致新增失败），比成功率更有说服力。
- **动作**：定义口径 —— 在一次 Run 中，若某次工具调用的意图可追溯到某条 PRIOR 且结果为失败/死路，
  记为一次 `prior_misled`；按 Run 聚合后计算比率，并在 A/B 汇总中作为负向指标报告。
  Worker 侧已有 `runtime.worker.knowledge_prior` 事件（含 `intent_id`）可作为关联锚点。
- **验收**：任取一个 on 臂 Run，能列出"被先验带偏"的具体步骤；off 臂该指标恒为 0。

### CAP-14　无效尝试 / dead-end 重复率

- **出处**：桌面方案 §11 第 6 条、§2.1 第 5 条（Muteki 的 dead-end 机制是其主要优势之一）。
- **现状**：**未做**。8ddbdb34 两臂分别有 10 / 7 个 `dead_ends`（off/on），但没有"重复踩同一死路"的比率。
- **动作**：基于 SharedGraph 的 dead-end 记录与 ToolCall 参数指纹，
  统计「同一 (技术路线, 参数形状) 被 ≥2 个 Worker 重复尝试」的次数与占比。
- **验收**：能输出 off vs on 的重复尝试次数与占比，作为知识增益的辅助证据。

### CAP-15　LearnedSkillCandidate 审核后发布与索引

- **出处**：桌面方案 §2.2 第 8 条 ——「有审核状态，但没有审核后发布和索引的完整链路」。
- **现状（本次核对）**：`backend/app/api/v1/learned_skills.py` 只有三个端点 ——
  `GET ""`（列表）、`POST /from-run/{run_id}`（生成候选）、`POST /{id}/review`（审核）。
  **没有 approve→发布→写入检索索引的端点或服务**；`review` 之后候选就停在那里。
- **动作**：
  1. 审核通过后把候选转换为 `KnowledgeItem`（`kind=case`），写入 `data/knowledge/` 或 DB 侧知识源；
  2. 触发检索器索引重建（注意：`knowledge_context.py` 现有进程级缓存 `_RETRIEVER_CACHE`，需配套失效机制
     —— `clear_retriever_cache()` 已存在，直接复用）；
  3. 保留 `provenance.source_type=learned` 与原始 run 引用，确保可回溯。
- **验收**：审核通过一条候选后，下一次检索能召回它，且能溯源到 `source_run_id`。

### CAP-32　执行验证轨迹闭环（CTF-Dojo 式：跑靶场 → 拒绝采样 → 蒸馏 → 召回）

- **出处**：CTF-Dojo（参考文献 [3]，`docs/references.md:14`，arXiv 2508.18370 / ICML 2026）。
  其核心结论是「**执行验证信号远优于合成数据**」——只用 486 条 **flag-verified 轨迹**做拒绝采样，
  即取得跨三个 benchmark 的 +11.6% 绝对提升（32B 达 31.9% Pass@1）。
  本项目已有同样的三段零件（蒸馏 → 模板库 → 召回器），但**三段都没接上**。
- **实证现状（本轮核对，闭环三段全断）**：
  1. **写端目录错位**：`muteki_runtime.py:1007` 的 `_distill()` 把模板写到
     `Path(workspace_path).resolve().parent.parent / "knowledge"`，按 `services/workspace.py:16` 的
     `workspace_root / run_id` 规则解析，实际落点是 **`backend/data/knowledge`** —— 该目录**当前不存在**
     （`find backend/data -type d -name knowledge` 无结果，`data/knowledge/*.yaml` 也无）。
     `TemplateStore.__init__` 会 `mkdir(parents=True, exist_ok=True)`，说明**从未成功走到 `store.save`**。
  2. **静默失败**：`_distill()` 整段包在 `except Exception` 里，只打一条 `muteki.distill.failed` warning；
     而 `data/logs/backend.err.log` 中 **零条** `muteki.distill.*` 记录
     （唯一命中的 `distill` 是 SQLAlchemy 的 `distilled_parameters`，无关）
     → 无法区分「根本没跑到」与「跑到就炸了」。调用点仅一处：`muteki_runtime.py:334`，
     门槛为 `result.flag_found and result.graph_path`。
  3. **读端零调用点**：`app/services/retrieve.py` 的 `TemplateRetriever` / `build_retriever()`
     **全仓库零引用**（含 `backend/tests`、`docs`），是死代码。即模板**即使写对了也永远不会被召回**。
- **与 CURATED 通道的关系**：`app/knowledge/retriever.py` 走的 BM25 只读 `data/knowledge/*.jsonl`
  （25 条手写语料），**不读** `TemplateStore` 的 yaml → 两条知识通道当前完全割裂。
  语义上必须区分可信级别：BM25 语料是人工整理（`reviewed`），模板是本项目自产轨迹（未核验）。
- **动作**：
  1. **写端**：`_distill()` 目标目录改为 `C7F_KNOWLEDGE_ROOT/templates/`（与 CURATED 同根、子目录隔离），
     并允许配置覆盖；不再用 `workspace_path` 反推知识目录；
  2. **失败可见**：异常改为结构化告警（含 `error_type` / `graph_path` / `run_id`），计入健康指标，
     不允许静默吞掉；
  3. **读端接入**：把 `TemplateRetriever` 挂进 `KnowledgeContextService`，与 BM25 结果统一排序后下发
     （复用本轮已建好的 `format_worker_prior()` 动作导向格式），并在 prior 文本中标注
     「本项目自产，可信级别低于人工整理语料」；
  4. **拒绝采样口径**：仅对 `flag_found AND 判题器 PASS` 的 Run 蒸馏为**正向**模板；
     失败 Run 蒸馏为 `kind=negative`（正好补 CAP-12 缺失的 negative 类型）；
  5. **索引失效**：写完模板后调 `clear_retriever_cache()`，避免进程级缓存读到旧索引。
- **验收**：跑完一个 solved Run 后 `data/knowledge/templates/` 出现对应 yaml；
  下一个同类挑战的 Worker prior 中能出现该模板，且能溯源到 `source_run_id`。

### CAP-33　评测口径补 Pass@k / 首解率 / 预算内成功

- **出处**：CTF-Dojo 的对外口径是 **Pass@1**（首次尝试即抓 flag）+ 每挑战 **交互预算约 40 轮**，
  这样其数字才能与 InterCode-CTF / NYU CTF Bench / Cybench 横向对比。
- **现状**：`app/benchmark/evaluation.py:164` 只产出 `SUCCESS / PARTIAL / INCOMPLETE` 三态
  （判定依据是 `flag_candidates(verified=True, review_state=VALID)` 或 `finding_created`），
  **没有**「第几次尝试成功」「是否在预算内成功」「耗了几轮」的字段
  → 既无法与外部 benchmark 数字对齐，也无法衡量"同样解出但更快"。
- **动作**：在 `evaluate_run()` 输出中补 `first_success_attempt` / `solved_within_budget` / `turns_used`，
  预算值由 `BenchmarkCase` 携带（`case_definition.py` 加可选字段，保持 `extra="forbid"` 兼容）；
  聚合层补 Pass@k（k=1 起步）与 turns_used 分布。
- **验收**：同一 case 多次运行可输出 Pass@1 与 turns_used 分布，字段口径与论文一致，可直接对表。

### CAP-34　运行时随机化（防过拟合）

- **出处**：CTF-Dojo 的 runtime augmentation —— 随机化端口、文件路径、服务域名与 flag，
  论文实测「augmenting the runtime environment 能让模型解出更多 CTF 挑战」。
- **现状**：本项目的 target 是**硬编码常量** —— 例如 `cases/sql-injection-golden.json` 的
  `"target": "http://192.168.236.1:8092/search?asset_no="`，flag 亦为固定值。
  这给"背地址 / 背端点 / 背 flag"留了捷径，任何成功率都可能被捷径污染，
  且与 CAP-03 要求的「靶场重置」互为前提。
- **动作**：靶场起停时随机化宿主端口与 flag 值（`ranges/asset-warranty-mysql/docker-compose.yml` 的
  `.env` 已支持端口变量，可直接复用），由 case 定义的 `{{target_url}}` 模板注入；
  实验记录每次实际取值，确保结论可复述。
- **验收**：同一 case 连跑两次，端口与 flag 不同，而 Agent 成功率不出现系统性下降（即无地址记忆）。

---

## 4. P2 —— 需要逐环节取证才能动手

### CAP-20　Worker 事实向外层投影的完整性

- **出处**：桌面方案 §2.2 第 3 条 ——「Worker 内部事实向外层 ToolCall、EvidenceLedger、VerifiedFact 的投影不完整」。
- **为什么是 P2**：这是判断题，不是执行题。需要先取一次真实 Run 的完整链路
  （Worker 内部 fact → `ToolCall` → `EvidenceLedger` → `VerifiedFact`），逐环节比对丢失点。
- **动作**：写一个只读审计脚本（不改业务逻辑）输出投影链路对照表，再决定改哪一环。
- **验收**：得到一张"哪一类 Worker 事实在哪一环丢失"的清单。

### CAP-30　Reason/Coordinator 动态扩容与策略深度

- **出处**：桌面方案 §2.2 第 4 条。
- **状态**：**本次不做**。与协调器议题耦合（含容器化、独立并发额度、provider 渠道隔离），等协调器方向定了再单列。

### CAP-31　批量实验矩阵 runner + 靶场重置

- **出处**：桌面方案 §8.2、§12 阶段 3/4。
- **状态**：与 CAP-03 强耦合，建议合并推进；单独评估 `ranges/asset-warranty-mysql` 之外的靶场是否可稳定重置。

---

## 5. 明确不做（本次范围外）

1. 完整复刻 Muteki（含协调器容器化）。
2. embedding / cross-encoder reranker / 向量数据库（方案 §7.2 已明确第一版不做）。
3. Wiki 后台、权限系统、自动知识发布（CAP-15 的"人工审核后发布"不算自动发布）。
4. 无人工审核地抓取全网 write-up。
5. 用单一总成功率宣称跨题型泛化。
6. **复现 CTF-Dojo 的拒绝采样 SFT 微调**（论文 §Training：LoRA/SFT 7B-32B、lr 5e-6）——
   本项目模型为第三方 API（不可微调）、无 GPU 训练栈，且 pwn.college 语料以二进制/逆向/密码学为主，
   与 Web 业务靶场分布不匹配。**只引用其数字作 baseline 对照，不投入实现**；对应做法改用 CAP-32 的自产知识闭环替代。
7. **整体照搬 CTF-Dojo 的 658 道题** —— 只取其方法学断言（flag-verified 轨迹回灌、writeup 作提示）
   与 CTF-Forge 的容器化思路（并入 CAP-31），不引入其题库。

---

## 6. 建议的第一批动作（5 个）

按"投入小 × 能解锁后续"排序：

| 顺序 | 事项 | 理由 |
|---|---|---|
| 1 | **CAP-32 执行验证轨迹闭环** | 现状是「三段零件全在、但一段没接上」：写端目录错位、异常被静默吞掉、读端零调用点。改动集中在 2 个文件，却是**唯一一条"项目自己越跑越强"的机制**；不修则 CAP-12/15 的一切自产知识都无处落地 |
| 2 | **CAP-10 重试加 jitter** | 半天量级、纯函数改动、有现成测试文件可扩；直接降低"4 并发长请求被 provider 5xx 打死"的概率，而那是已实际发生过的事故模式 |
| 3 | **CAP-02 召回明细持久化** | 不做这个，后面所有知识效果分析都只能靠"重建 query 猜测"；本轮 Worker 侧的事件已铺好一半 |
| 4 | **CAP-01 + CAP-04 evaluate 前置校验与语料时间去伪** | 不修这两条，任何 A/B 结果都会被"没绑评测器"和"占位日期"两点否掉 |
| 5 | **CAP-33 + CAP-34 口径与随机化** | 让本项目的数字能与论文/外部 benchmark 对表（Pass@1），并消除"背地址"捷径 |

完成前四项后，CAP-03 的批量实验才值得投入算力去跑。

---

## 7. 佐证文件索引

| 内容 | 路径 |
|---|---|
| 知识增强与评测落地方案（最全的"怎么做"） | `D:\desktop\C7F-W3B-Q1LL3R-知识增强与评测落地方案.md` |
| 协调器事故根因 + 4 条调优建议 | `D:\desktop\codex\c7f-coordinator-reason-outage\CONTEXT.md` §5 |
| 知识增强已实现/未实现边界 | `D:\desktop\codex\c7f-knowledge-augmentation-strategy\CONTEXT.md` §4 |
| 单次 A/B 不可用复盘 | `D:\desktop\codex\c7f-experiment-8ddbdb34-analysis\CONTEXT.md` §5/§6 |
| 配对实验后端 | `D:\desktop\codex\run-control-experiment\CONTEXT.md` |
| 策略反馈闭环实证 | `D:\desktop\codex\attack-strategy-feedback-real-run\CONTEXT.md` |
| 方法论漏洞修复结论 | `D:\desktop\codex\outputs-2026-07-12--methodology-repair\CONTEXT.md` |
| 语料 | `backend/data/../data/knowledge/*.jsonl`（25 条） |
| 检索器 | `backend/app/knowledge/retriever.py` |
| 知识上下文服务 | `backend/app/services/knowledge_context.py` |
| 重试退避 | `backend/app/engines/retry.py` |
| 题解经验（同题） | `backend/app/services/challenge_lessons.py` |
| 技能学习候选 | `backend/app/api/v1/learned_skills.py` |
| **CTF-Dojo（参考文献 [3]）** | 论文 arXiv 2508.18370 / ICML 2026 poster；官方仓库 `github.com/amazon-science/CTF-Dojo` |
| 执行验证轨迹蒸馏（写端） | `backend/muteki/learning/distill.py`（`Template` / `TemplateStore` / `distill_from_events`） |
| 蒸馏触发点（写端调用） | `backend/app/solver/muteki/runtime/muteki_runtime.py:334`（`_distill` 定义在 `:986`） |
| 模板召回器（**死代码**） | `backend/app/services/retrieve.py`（`TemplateRetriever` / `build_retriever`，零调用点） |
| 评测器 | `backend/app/benchmark/evaluation.py`（三态口径，无 Pass@k） |
| 用例定义/加载 | `backend/app/benchmark/case_definition.py` / `case_loader.py` / `cases/*.json`（仅 4 个用例） |
| 靶场 | `ranges/asset-warranty-mysql/`（当前**唯一**靶场） |

---

## 8. CTF-Dojo 式训练法 → 本项目落地对照

> CTF-Dojo（[3]）的"训练"有四种形态，**只有第二种是你现在能直接做的**。
> 下面逐条给出对照，避免把"论文的训练法"照搬成一个跑不动的计划。

### 8.1 四种形态与本项目适配度

| CTF-Dojo 的形态 | 论文做法 | 本项目适配度 | 结论 |
|---|---|---|---|
| ① 拒绝采样 **SFT 微调模型** | 658 挑战跑轨迹，只留 flag-verified 的 486 条，LoRA/SFT 7B/14B/32B，lr 5e-6 | **不可行**。本项目模型走第三方 API（不可微调），无 GPU 训练栈；且 pwn.college 语料以二进制/逆向/密码学为主，与 Web 项目分布不匹配 | **学位论文里作"受限未采用"的对比方案**，引用其数字作 baseline，不投入实现 |
| ② **用它的方法学闭环做自产知识** | 执行验证信号 > 合成数据；writeups 作提示相对提升最高 64% | **最适合**。仓库里 `distill.py` + `TemplateStore` + `TemplateRetriever` + `LearnedSkillCandidate` 四个零件**都已存在**，只差接线（CAP-32） | **主线** |
| ③ **用它的评测口径** | Pass@1 + 每挑战约 40 轮交互预算 | 目前只有三态，无法对表 | **补口径**（CAP-33） |
| ④ **用它的靶场工程** | CTF-Forge 自动 Docker 化，0.5 秒/题，98% 可复现；runtime 随机化防过拟合 | 本项目仅 1 个靶场 + 4 个 case，target 硬编码 | **靶场规模化 + 随机化**（CAP-31 / CAP-34） |

### 8.2 三条关键发现对本项目的直接含义

1. **「策略发现」比「技术执行」更难** —— 论文实测：把人类 writeup（flag 已 redact）当提示，
   成功率相对提升最高达 **64%**。
   → 直接支持本项目的 **PRIOR 而非答案**设计，也说明 **CAP-12 应优先补 `kind=case`（策略级）
   而不是继续补 `kind=poc`（技术级）**：现在 25 条里 `technique` 16 + `poc` 9，`case` 与 `negative` 均为 0。
2. **环境随机化能提升解出数，不只是防作弊** ——
   → CAP-34 不只是"实验卫生"，它本身就是能力增益手段。
3. **教师模型多样性 > 单一强模型** ——
   → 与本项目多引擎架构（`docs/archive/2026-07/multi-agent-core.md`、`multi-chain-solver-analysis.md`）天然契合；
   可作为"为什么不用单一大模型"的论证材料。

### 8.3 一句话定位

**CTF-Dojo 对本项目的价值不在"658 道题"，而在它的两个方法学断言**：
(i) 只有 flag-verified 的轨迹值得回灌成知识 —— 这正是 CAP-32 的拒绝采样口径；
(ii) 人类 writeup 作提示能大幅提升成功率 —— 这正是本项目已建好的 PRIOR 通道。
**658 道题本身**（pwn.college 语料，二进制/逆向/密码学为主）与 Web 业务靶场的分布差异较大，
不适合整体照搬；**CTF-Forge 的自动容器化思路**倒是可以直接用于扩容自己的 Web 靶场（CAP-31）。

---

## 9. 执行记录：可自动化部分已落地

> 口径：只列**已经改完并跑通测试**的；需要人工决策/外部信息的全部下沉到 §9.2。

### 9.1 已完成（CAP-32 / CAP-10 / CAP-02 / CAP-01 / CAP-33 / CAP-04 一半）

| CAP | 改动位置 | 关键内容 |
|---|---|---|
| **CAP-32** | `app/solver/muteki/runtime/muteki_runtime.py`、`app/solver/muteki/upstream_bridge.py`、`app/services/retrieve.py`、`app/services/knowledge_context.py`、`muteki/learning/distill.py`、`muteki/swarm/shared_graph.py`、`app/knowledge/config.py` | 三段全通，见 §9.3 |
| CAP-10 | `app/engines/retry.py`、`app/engines/openai_compatible.py` | `backoff_delay(..., jitter=, rng=)`，默认 `jitter=0.0` 保持历史值逐字节不变；引擎默认 `0.5`；`Retry-After` 为抖动**不可下穿**的地板；5 处瞬时重试全走 `self._backoff()`，解析修复重试保持确定性 |
| CAP-02 | `muteki_runtime.py`（Reason 选模处） | `REASON_MODEL_SELECTED` 现落 `knowledge_query`(≤2000) / `knowledge_top_k` / `knowledge_items`(id,kind,score) / `knowledge_item_ids` / `knowledge_signals` |
| CAP-01 | `app/services/run_control_experiment.py` | 新增 `_ensure_source_challenge_is_evaluatable()`；challenge 缺 `metadata_json["benchmark_case_id"]` 时报 `SOURCE_CHALLENGE_NOT_EVALUATABLE`(**409**)，从源头禁止产出无法判分的配对 |
| CAP-33 | `app/benchmark/case_definition.py`、`evaluation.py`、`__init__.py` | 报告补 `attempt` / `turns_used` / `turn_budget` / `solved_within_budget` / `verified_flag_rank`；新增 `aggregate_pass_at_k()`（空样本返回 `None`，**绝不伪造 0.0**） |
| CAP-04（半） | `app/knowledge/config.py`、`retriever.py`、**新增** `app/knowledge/audit.py` | `C7F_KNOWLEDGE_REQUIRE_PUBLISHED_AT` + `require_published_at` 检索排除 + `_env_flag()` 严格校验；`python -m app.knowledge.audit` 作为可运行告警（不可用则 exit 1） |

#### CAP-32 细节：闭环为什么原来是断的

1. **写端目录错位** —— `_distill()` 用 `workspace_path` 算术推目录，实际落到 `backend/data/knowledge`（不存在）；
   改为统一入口 `knowledge_templates_root()`（= `<knowledge root>/templates`，可用
   `C7F_KNOWLEDGE_TEMPLATES_ROOT` 覆盖），**写端与读端共用同一函数**，杜绝再次错位。
2. **真正的根因（此前一直没查出来）** —— `muteki.swarm.shared_graph.SharedGraph` 是 `typing.Protocol`，
   旧代码 `SharedGraph(Path(...), challenge_id=...)` **每次调用都抛 `TypeError`**，
   被裸 `except` 吞掉 → 所以从未成功写出过模板。现改为
   `open_upstream_shared_graph_readonly()`（只读打开，禁止 migrate/checkpoint），
   并把 `except` 改成结构化 `muteki.distill.failed`（含 `error_type` / `graph_path` / `traceback`）。
3. **读端零调用点** —— `TemplateRetriever` 全仓库无引用。现接入 `KnowledgeContextService.build()`
   与 `build_worker_prior()`，作为**追加在人工语料之后、显式标注低可信**的块
   （Reason 侧 ≤600 字符，Worker 侧共享预算）；召回异常一律降级为空串，绝不阻塞解题步骤；
   保存后调 `clear_retriever_cache()`。
4. **拒绝采样口径** —— `solved=True` → `kind="case"`；`solved=False` →
   新的 `distill_negative_from_events()` 出 `kind="negative"`（来源 `dead_end_reasons()` +
   REFUTED 假说，flag 已脱敏）→ 补上 CAP-12 缺的 negative 类型；
   图文件不存在直接跳过（`reason="graph_missing"`），不制造垃圾模板。
   `Template` 新增 `kind`，但 `to_yaml()` **仅在 `kind != "case"` 时输出**，既有 case YAML 保持字节不变。

### 9.2 需要人工处理（按优先级）

| 优先级 | 事项 | 具体动作 |
|---|---|---|
| ~~**P0**~~ | ~~**CAP-04 日期回填**~~ | ✅ **已完成**：`2020-12-03` 经核实是 **OWASP WSTG v4.2 的真实发布日**，原判断（"20 条占位"）是审计启发式误报。`audit.py` 改为按**归属**判断（同文档共享版本日期合法、跨文档才算可疑），并回填 6 条错误日期。详见 §9.4 |
| **P0** | **CAP-03 批量实验** | 统计层已落地（§9.4）。仍需**跑一次配对矩阵**（≥10 对）才能摆脱 `descriptive_only` —— 这是算力动作，需要你决定何时跑 |
| ~~P1~~ | ~~**CAP-34 运行时随机化**~~ | ✅ **已完成**（端口 + flag）。见 §9.4。注意由此产生的**靶场地址口径更正**：真实是 `127.0.0.1:<WARRANTY_WEB_PORT>`（默认 28037），不是 README 的 28036、也不是 case 里旧写的 8092 |
| P1 | **CAP-12 语料扩容** | 需人工撰写。按 §8.2 结论：**优先补 `kind=case`（策略级）**，而非继续补 `kind=poc`（当前 25 条 = technique 16 + poc 9，case/negative 均为 0） |
| P1 | **`turn_budget` 赋值** | 现有 4 个 case JSON 均未声明预算 → `solved_within_budget` 恒为 `None`。论文口径约 40 轮，可按此设定 |
| P1 | **CAP-11 跨题案例库** | `challenge_lessons` 现只查同 `challenge_id` |
| P1 | **CAP-15 发布/索引链路** | `LearnedSkillCandidate` 有 review、无 publish/index |
| P2 | **CAP-31 / 靶场扩容** | 现仅 1 个靶场 + 4 个 case（`sql-injection-golden` / `sql_injection_boolean` / `ssrf-internal` / `xss-reflected`） |

### 9.3 验证证据（本次实测）

- 本次新增/修改的 8 个测试文件：**58 passed**
  （`test_retry_backoff_jitter` / `test_openai_compatible_chat_retry` / `test_muteki_distill_terminal_order` /
  `test_knowledge_learned_prior` / `test_run_control_experiment` / `test_run_control_experiment_hardening` /
  `test_benchmark_pass_at_k` / `test_knowledge_published_at`）。
- 焦点回归（`knowledge|benchmark|distill|control_experiment|retry|...` 过滤）：**160 passed / 3 failed**；
  3 个失败经 **HEAD 干净 worktree 对照**确认为既有失败，与本次改动无关：
  - `test_knowledge_api.py` ×2 —— 该文件在 HEAD 中**不存在**（纯用户 WIP）；
  - `test_solver_action_recovery.py` —— 该文件在 HEAD 上 **4/4 全挂**。
- `ruff check backend/app backend/muteki backend/tests` → **All checks passed!**；`compileall` → exit 0。
- 真实语料审计实跑结果（**已更正**）：见 §9.4 —— 原记录的"20 条占位日期"是审计启发式误报。

---

## 10. 执行记录（第二轮：P0/P1 全量开工）

### 10.1 靶场真相（实测，推翻多处文档记载）

| 说法 | 实测 |
|---|---|
| README：宿主 `28036` | ❌ 过期 |
| `docker-compose.yml`：`127.0.0.1:28037:5000` | ✅ **真实值**（`netstat` 确认唯一监听） |
| case 旧写法：`192.168.236.1:8092/search?asset_no=` | ❌ 端口与路由**都不存在** |
| Runner VM `192.168.236.128:8091` | ❌ 不可达（VM 未开） |
| `backend/.env` 的 `APP_RUNNER_URL` | ✅ `http://127.0.0.1:8091` → **runner 是宿主进程，不在容器/VM 里** |

- **不存在额外端口映射机制**。`192.168.236.1:28037` 返回的 `502` 是本机
  `http_proxy=127.0.0.1:3086` 在应答，**不是**代理映射 —— 排查此类问题必须加 `curl --noproxy '*'`。
- 靶场真实路由只有 `/`、`/health`、**`POST /api/warranty/check`**
  （`asset_no` 参数化绑定；`department` 是唯一注入点；返回 `{matched: bool}` 布尔 oracle）。
- **4 个 case 描述的路由靶场一个都没实现**（`/search?asset_no=`、`/search?q=`、`/fetch?url=` 全是虚构）。
  2 个 SQLi case 已修正到真实 endpoint；`ssrf-internal` / `xss-reflected` 需本仓库不存在的靶场，
  已按决策 3 标 `requires_range: "web-vuln-lab"` **跳过而非失败**。
- flag 在 `challenge_settings.verification_token`，由 `002-seed.sh` 以
  `ON DUPLICATE KEY UPDATE` 播种 → **改 flag 无需重置数据卷**，re-exec 该脚本即可。

### 10.2 已完成

| 项 | 落点 | 关键内容 |
|---|---|---|
| **CAP-34** | 新增 `app/benchmark/range.py`、`experiment.py`、`docker-compose.yml`、`.env`、2 个 case | `{{host}}`/`{{port}}` 占位符（复用 `SensitiveValueSanitizer` 已有约定）；`range_endpoint()` 优先级 **env → 靶场 `.env` → 默认**；`bring_up()` 一次完成换端口 + 换 flag + 重播种 + 等健康。**实测新端口 8383、health ready** |
| **CAP-34 根因** | `experiment.py::_get_or_create_challenge()` | 原本按 `benchmark:{case_id}` 查到旧 challenge 就**提前 return**，随机化后的新端口**永远传不到 challenge 上**。现改为 target 变化时刷新 `target_url` 与 `allowed_hosts` |
| **CAP-03 统计层** | `experiment.py` | `_exact_mcnemar_p()` 用 stdlib `math.comb` 做**精确**双侧检验；`_bootstrap_mean_ci()` 固定种子 20260101/2000 次以保证可重算；**样本 <10 对时不下显著性结论**；`_arm_statistics()` 补 `pass_at_1`（分母只用已评测尝试）与 `turns_used_distribution` |
| **CAP-04** | `app/knowledge/audit.py`、`data/knowledge/*.jsonl` | 启发式改为按**归属**判断；回填 6 条错误日期；**结果 25 条 / 8 个日期 / 0 缺失 / 0 可疑 / `time_split_usable: True`** |
| 无靶场 case | `case_definition.py`、`experiment.py` | 新增 `requires_range`；跳过写入 `plan.json` 与 `summary.json`，`status="SKIPPED_REQUIRES_RANGE_MISSING"`，避免"跳过的题被算成解不出" |

### 10.3 CAP-04 的更正（重要）

**`2020-12-03` 不是占位日期**，它是 **OWASP WSTG v4.2 的真实发布日**（官方 downloads 页
`Version 4.2  December 3, 2020`）。19 条条目引用同一份文档，共享版本日期是**合法**的。
原判断是审计启发式的误报 —— 而按那个误报去"修"，只有一个出路就是**编造每条不同的日期**，
正是本项目明令禁止的。查证报告：`docs/knowledge-provenance-review.md`。

回填的 6 条：`sqli-union-based`→2019-04-02、`ssti-jinja2-detection`→2015-08-05、
`jwt-key-confusion`→2015-03-31、`ssrf-cloud-metadata`→2011-09-28、
`client-template-injection`→2016-01-27（**原引用本身就是错的**：WSTG v4.2 无该测试项，v5 才有）、
`java-deserialization-gadget`→2017-11-20（锚 OWASP Top 10 2017 A8）。

新审计的残余限制会**如实输出为 caveat**：18/25（72%）同源于 `owasp wstg v4.2`，
所以时间留出切分只能区分该版本的年代，**不能回答"某技术在某题之前是否已公开"**。

### 10.4 仍需人工 / 后续

| 优先级 | 事项 | 说明 |
|---|---|---|
| P0 | **跑配对矩阵** | 统计层已就绪，但需要算力与靶场时间；≥10 对才脱离 `descriptive_only` |
| P1 | CAP-12 `kind=case` 语料 | 已起草 6 条（`data/knowledge/cases.jsonl`，`trust_level=unverified`），**待你审核后改 `reviewed`** |
| P1 | CAP-03 CLI + API 入口 | ✅ 已完成（§12.2） |
| P1 | CAP-11 跨题案例库 / CAP-13 prior_misled / CAP-14 dead-end 重复率 / CAP-15 发布索引链路 | ✅ 均已落地（§11、§12），实测数据待配对矩阵 |

---

## 11. 剩余四项：具体做法

> 本节为代码核对后的落地方案（每条都注明了真实文件与行号依据），可直接照做。
> 结论性的"要改哪一行"写在表里；需要你拍板的集中在本节末尾。

### 11.0 前置事实（本轮核对，全部实证）

| 事实 | 依据 |
|---|---|
| `ExperimentRunner` / `RealRunExecutor` **零生产调用点** | 全仓库引用只有 `tests/test_benchmark_experiment.py`、`tests/test_benchmark_range.py` |
| 实验的**只读 API 已存在** | `GET /knowledge/experiments`、`GET /knowledge/experiments/{id}`（既读落盘目录也读 DB 配对）→ 缺的只是**触发与状态** |
| `ToolCall` 表**没有 `intent_id` 列** | `models/run.py:319-339` 只有 `turn_id` / `logical_tool_call_id` / `agent_task_id` → 「先验 → 具体失败工具调用」**无法靠 DB join** |
| 先验事件写在 **SharedGraph**，不是 `run_events` | `muteki_runtime.py:733-746` 走 `self._graph.emit_event`，落 `workspace_path/muteki/graph/upstream_shared_graph.db`；读法：`SharedGraph.events_since(kinds=)` / `SQLiteSharedGraph.open_readonly` |
| `Challenge` **没有题型字段** | `models/challenge.py:9-27`；`vulnerability_type` 只在 `benchmark/cases/*.json`，靠 `metadata_json["benchmark_case_id"]` 关联 |
| `provenance.source_type` 是**自由字符串** | `knowledge/models.py:12`（非枚举）→ `derived` 可直接用，**无需改 schema** |
| `trust_level` 已含 `unverified`；loader 对 `case` **无额外校验** | `models.py:34`、`loader.py:111-116`（只对 `negative` 强制 `known_failures`） |
| 应用库 `127.0.0.1:3307` **当前未监听** | `netstat` 无结果 → CAP-11/13/14 的"在有数据 run 上验证"必须先起库 |

### 11.1 CAP-03　CLI + API（批量矩阵入口）　✅ 已落地

| 要做的 | 落点 |
|---|---|
| CLI 入口（house style = `def main() -> int` + `raise SystemExit(main())`，同 `app/knowledge/audit.py`） | **新增** `backend/app/benchmark/__main__.py` |
| 参数 | `--case-dir` `--output-dir` `--repeats` `--arms` `--resume` `--dry-run` `--worker-model` `--reason-model` `--randomize-range` `--json` |
| 组装 | `RealRunExecutor(...)` + `ExperimentRunner(...)` → `asyncio.run(runner.run())` |
| 随机化接入 | 直接复用 `app/benchmark/range.py::bring_up()`；`resolve_case_target()` 已以靶场 `.env` 为准，**写完即生效，无需透传 endpoint** |
| 早退门禁 | 先 `await executor.preflight()`：非 `ready` 立即退出（否则会跑出一堆 `MODEL_CONFIG_REQUIRED`）；DB 不可达同样在此拦下 |
| 退出码 | `0` 完成 / `2` 配置错 / `3` 存在失败任务 → 可进 CI |
| API 触发 | **新增** `POST /knowledge/experiments`：校验（`output_dir` 必须在 `backend/data/experiments` 下、`repeats≥1`、`arms ⊆ ARMS`）→ 写 `run_request.json` → 起后台任务 → 立即返回 `{experiment_id, status}` |
| API 状态 | **不新造状态库**：`state.json`（pid/起止/退出码）+ 已有 `plan.json` / `raw/*.json` 算 `completed/total`；存活判断复用 `experiment.py` 已有 `_process_is_running()` / `_lock_owner()` |
| 重启风险（必须承认） | `asyncio.create_task` 的句柄随进程消失 → 只能靠 `state.json` 判孤儿，并用 CLI `--resume` 接手 |

**验收**：`python -m app.benchmark --dry-run` 只写 `plan.json` 不执行；`--resume` 跳过已完成任务；同一 manifest 重跑输出结构一致。

### 11.2 CAP-11　跨题案例库　✅ 已落地

**为什么不能直接聚合**：`Challenge` 无题型字段，`vulnerability_type` 只存在于 case JSON。

| 步骤 | 落点 |
|---|---|
| 抽出共用统计口径（现在内联在 `extract()` 里，`challenge_lessons.py:28-53`） | 新增私有 `_aggregate(session, runs)`，同题/跨题两处共用 |
| 新增跨题视图 | `extract_by_profile(session, *, vulnerability_type, target_stack=(), exclude_challenge_id=None, limit=20)`：按 `metadata_json["benchmark_case_id"]` → case 的 `vulnerability_type` 找同题型 challenge，再取它们的 `COMPLETED_SOLVED` run |
| 聚合键与语料统一 | `BenchmarkCase` 加可选 `target_stack: list[str]`（与知识语料同词表），避免再造一套词表 |
| 下发通道 | **不塞 context builder 固定字段**：产出低可信先验块，与 CAP-32 的 learned prior 共用 `app/services/retrieve.py` 的渲染器（泛化为"自产先验块"） |
| 边界标注 | 文本必须写明「本项目自产，可信级别低于 reviewed 语料」 |

**验收**：无同题历史的新题（`exclude_challenge_id`）也能拿到按题型聚合的先验，且能区分自产 / 人工整理。
**注意**：未绑 `benchmark_case_id` 的挑战**无法参与跨题聚合** —— 这是 CAP-01 门禁的第二个收益。

### 11.3 CAP-13　`prior_misled`　✅ 已按 A 口径落地

| | **A：只读事件（建议先做）** | **B：运行时补事件** |
|---|---|---|
| 口径 | 某 intent 收到过带 `item_ids` 的 prior，且该 intent 结局为 `DEAD_END` 或 `INTENT_CONCLUDED(result∈失败集)` → 计 1 次 | 补发 `runtime.worker.tool_result`（含 `intent_id`/`tool_call_id`/`status`），口径下沉到**具体失败的工具调用** |
| 改动面 | **零侵入**，只读图事件（`events_since(kinds=[...])`） | 动 runtime 主路径 + 新增事件类型（影响所有 run） |
| 精度 | intent 级 | 工具调用级 |
| 风险 | 可能把"本来就注定失败的路线"也算进来 → 必须加归因护栏 | 事件量增加；需回归全部 muteki 测试 |

**归因护栏（无论选 A/B 都要有）**：
- 必须同时给出 **off 臂恒为 0 的证明**；off 臂无 prior 事件 → 分母为 0 时报 `null`，**不许报 `0`**（否则看起来"完美"）。
- 每条 `prior_misled` 要能重放：记录 `intent_id` + 命中 `item_ids` + 结局事件 seq + prior 文本片段，供人工判定是否真被带偏。

**数据锚点**：`DEAD_END` payload 含 `description`（`muteki/graph.py:146-147`）；`INTENT_CONCLUDED` payload 含 `result`（`graph.py:465`）；`knowledge_prior` 事件含 `intent_id` + `item_ids`（`muteki_runtime.py:739`）。

### 11.4 CAP-14　无效尝试 / dead-end 重复率　✅ 已落地

**已有零件**：`app/services/action_fingerprint.py::build_execution_fingerprint(tool_name, arguments)`（含 request/payload 规范化、剥离 `STRATEGY_METADATA_KEYS`）+ `fingerprint_action()`；`challenge_lessons.py:36` 已有"参数形状"写法 `tool{sorted(arg_keys)}`。

| 步骤 | 落点 |
|---|---|
| 一个文件同时装 CAP-13 / CAP-14（避免两个模块各建一遍 DB 往返） | **新增** `backend/app/services/run_process_metrics.py` |
| 重复口径 | 按 `(tool_name, execution_fingerprint, argument_shape)` 分组 `ToolCall`，`count ≥ 2` 记重复 → `repeat_calls / total_calls`、`repeat_groups[]`（含 fingerprint、次数、是否最终 FAILED） |
| 先验口径 | `compute_prior_misled(graph_path, run_id)`：只读打开图（复用 §9 新增的 `open_upstream_shared_graph_readonly`） |
| 融入实验 | `RealRunExecutor.execute()` 返回前把 metrics 塞进 `ExecutionResult.metadata` → 自动流入 `_raw_record()["metadata"]` → `_arm_statistics()` 按臂求均值。**零额外 DB 往返，summary 仍可由原始日志重算**（符合 §0 硬约束 1） |
| 回溯老 run | `python -m app.services.run_process_metrics <run_id>...`（或并进 CAP-03 CLI 的 `--explain`） |

**验收**：能输出 off vs on 的重复次数与占比；8ddbdb34 那批老 run 可回溯算出。

### 11.5 CAP-15　LearnedSkillCandidate 发布与索引　✅ 已落地

| 步骤 | 落点 |
|---|---|
| 新增 `publish(session, candidate_id)` 服务方法 | `app/services/learned_skills.py`（现有只有 `create_from_run` / `review`） |
| 前置校验 | 只有 `status == "APPROVED"` 才可发布；`security_scan_json.passed` 为真 |
| 转换 | → `KnowledgeItem(kind="case", trust_level="curated")`，复用 `SensitiveValueSanitizer` 已有的占位符约定 |
| `published_at` 语义 | 取 **source run 的完成日期** —— 这不是编造，而是"该知识实际产生的日期"，且正好是时间留出实验需要的语义（自动蒸馏的模板才该是 `null`/排除） |
| 落盘 | `data/knowledge/learned.jsonl`（单独文件，不混入人工语料 3 个文件） |
| 索引失效 | 写完调 `clear_retriever_cache()`（`knowledge_context.py` 已存在） |
| 溯源 | 保留 `provenance.source_type="learned"` + `source_run_id` |
| **与 CAP-32 的分界** | CAP-32 模板 = 自动蒸馏、`unverified`；CAP-15 条目 = **人工审核过**、`curated`。两者共用检索通道但可信级别不同，必须在 prior 文本里区分 |
| **审计冲突（需一并处理）** | `audit.py` 的新启发式是"跨 `source_ref` 撞同一天才可疑"；多条 learned 条目若 `source_ref` 各不相同又同一天产出，会被误报为占位 → 需给 `source_type == "learned"` 单独分组 |

### 11.6 依赖顺序

```
CAP-03 CLI ──► 跑配对矩阵（≥10 对）──► CAP-13/14 才有可比较的 off/on 数据
CAP-11 ─┬─► 独立，但需先起应用库（3307）+ 存在跨题成功 run
CAP-12 ─┼─► 独立，纯内容，可与上并行
CAP-15 ─┴─► 依赖 CAP-32 已通的检索链（已完成），可与 CAP-12 并行
```

**最短路径**：先 CAP-03 CLI（解锁算力）→ 并行 CAP-14（纯读侧、无决策）→ CAP-12 起草（纯内容）→ 再 CAP-11 / CAP-13 / CAP-15。

---

## 12. 执行记录（第三轮：剩余四项全部落地）

### 12.0 本轮拍板的四个口径

| 待决项 | 你的选择 | 落地表现 |
|---|---|---|
| CAP-13 口径 | **A 只读图事件**（intent 级，零侵入） | `run_process_metrics.py` 只读打开图 DB，runtime 主路径零改动 |
| CAP-03 触发形态 | **CLI + API 起子进程** | `python -m app.benchmark` + `POST /knowledge/experiment-matrix` |
| CAP-11 聚合键 | **引入 `target_stack`，与语料同词表** | `BenchmarkCase.target_stack`（与 `data/knowledge/*.jsonl` 同一套字符串） |
| CAP-12 校验 | **`known_failures` 从 `negative` 扩展到 `case`** | `loader.py::_validate_cross_fields`；`publish()` 同步强制 |

### 12.1 新增 / 修改文件

| 文件 | 动作 | 作用 |
|---|---|---|
| `backend/app/benchmark/__main__.py` | 新增 | CAP-03 CLI（`--dry-run` / `--resume` / `--randomize-range` / 退出码 0/2/3/4） |
| `backend/app/services/benchmark_matrix.py` | 新增 | 矩阵作业控制（校验白名单、`state.json`、孤儿判定、`--resume` 提示） |
| `backend/app/api/v1/knowledge_experiments.py` | 新增 | `POST/GET /api/v1/knowledge/experiment-matrix` |
| `backend/app/services/run_process_metrics.py` | 新增 | CAP-13A（`prior_misled`）+ CAP-14（重复尝试），两种强度口径 |
| `backend/app/services/action_fingerprint.py` | 改 | 新增 `argument_shape()`，让课程与流程指标共用一套形状词表 |
| `backend/app/services/challenge_lessons.py` | 改 | 抽出 `_aggregate()`；新增 `extract_by_profile()` / `ProfileLessons` / `format_profile_lessons()` |
| `backend/app/services/knowledge_context.py` | 改 | `build()` / `build_worker_prior()` 接受并预算 `profile_prior`（第二个自产先验块） |
| `backend/app/services/context_builder.py` | 改 | `_profile_prior()`：**仅当本题无自身历史时**才下发跨题先验 |
| `backend/app/benchmark/case_definition.py` + `cases/*.json` | 改 | `target_stack` 字段；两个可跑靶的用例已填 |
| `backend/app/knowledge/loader.py` | 改 | `kind ∈ {negative, case}` 强制 `known_failures` |
| `data/knowledge/cases.jsonl` | **新增语料** | 6 条 `kind=case` 策略级条目，`trust_level=unverified` |
| `backend/app/services/learned_skills.py` | 改 | 新增 `publish()` + `PublicationCuration`（发布即写入 + 失效索引，失败回滚） |
| `backend/app/knowledge/audit.py` | 改 | `source_type == "learned"` 单独分组，不再被误报为占位日期 |

### 12.2 验收证据（本轮实测）

| 检查 | 结果 |
|---|---|
| `pytest test_run_process_metrics.py` | 14 passed；与配对统计/实验套件合计 **35 passed** |
| `pytest test_benchmark_matrix.py` | **27 passed**；`app.openapi()` 确认两条路由已注册 |
| `python -m app.benchmark --dry-run` | `4 task(s) planned`，`ssrf-internal` / `xss-reflected` 因缺靶场 `SKIPPED_REQUIRES_RANGE_MISSING` |
| `pytest test_challenge_profile_lessons.py` | **17 passed** |
| `pytest test_learned_skill_publish.py` | **20 passed** |
| 宽回归 `-k "knowledge or learned or lesson or benchmark or process_metrics or context_builder or retrieve or distill or matrix or challenge"` | **216 passed**，2 failed（见 §12.3） |
| `load_knowledge_items()` | 31 条 = technique 16 + poc 9 + **case 6** |
| `python -m app.knowledge.audit` | `time_split_usable: True`、`suspected_placeholder_count: 0`、`distinct_dates: 9` |
| `ruff check backend/app backend/tests` | `All checks passed!` |

### 12.3 已知遗留（必须由你处理，未静默修改）

| # | 事项 | 事实 | 建议 |
|---|---|---|---|
| 1 | `test_knowledge_api.py` 两条用例失败 | `list_knowledge_experiments` 在 **DB 不可达**时会额外返回 `db_error` 键；两条用例断言的是"完全相等"，因此**只要 MySQL 不在线就失败**。两个文件（`knowledge.py` / `test_knowledge_api.py`）均为 **2026-09-13** 所写，本轮未改动 → **属既有问题，非本轮引入** | 起 MySQL（3307）即可转绿；或把断言改为"只校验文件系统部分、容忍 `db_error`" |
| 2 | `SensitiveValueSanitizer` 的 `passed` 恒为真 | `sanitize()` 先替换再检查，四个正则都被替换过 → `scan["passed"]` 不可能为 `False`，`create_from_run` 里的 `QUARANTINED` 分支实际不可达 | 属既有可疑代码，**未改动**；若要修，应改为检查"替换后是否仍有 secret 形状残留" |
| 3 | CAP-12 语料可信级别 | 6 条 `case` 由我起草并引用了**已核实的真实日期**（NIST SP 800-115 = 2008-09-30、OWASP Top 10:2021 = 2021-09-24、以及语料中已有的 WSTG v4.2 = 2020-12-03 / RFC 7519 = 2015-05-01 / Top 10 2017 A8 = 2017-11-20） | 你过一遍内容，确认后把 `trust_level` 改 `reviewed` |
| 4 | 审计 caveat 仍会触发 | 21/31 条共享 WSTG v4.2 发布日（share 0.677 ≥ 0.5） | 这是**证据属性不是数据缺陷**；要消除只能引入更多不同发布日的文档 |
| 5 | 真实配对矩阵未跑 | 需算力 + 靶场 + 应用库（3307） | 跑 `python -m app.benchmark`，≥10 对后 `prior_misled` / 重复率才有可比数据 |

### 12.4 四条链路的用法速查

```bash
# CAP-03：计划一次矩阵（不执行）
python -m app.benchmark --case-dir backend/app/benchmark/cases --dry-run

# CAP-03：真跑（≥10 对才有统计效力），中断后接手
python -m app.benchmark --output-dir backend/data/experiments/<UTC> --repeats 10
python -m app.benchmark --resume --output-dir backend/data/experiments/<UTC>

# CAP-13/14：回溯任意老 run 的流程指标（只读图事件 + ToolCall）
python -m app.services.run_process_metrics <run_id> [<run_id> ...]

# CAP-12：审计语料日期能否撑起时间留出切分
python -m app.knowledge.audit
```

CAP-15 的发布是**代码路径**（`LearnedSkillService.publish`），需要 APPROVED 候选 + 审核人补齐 `known_failures` / `verification_method` 才会写入 `data/knowledge/learned.jsonl`；写入前会整库重载校验，失败即回滚，因此**不会污染语料**。
