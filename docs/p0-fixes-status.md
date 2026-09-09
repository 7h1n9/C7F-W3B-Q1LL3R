# P0 缺陷修复状态追踪

本文档追踪 README.md 中 P0 — 最高优先级缺失项的修复进度。

## 1. muteki-blackboard Skill（Worker 原生黑板读写通道）

**状态**: ✅ 已完成

**目标**:
- Worker 容器内预装 `muteki-blackboard` skill
- 提供标准化接口：`read_facts`, `write_fact`, `read_intents`, `write_intent`, `mark_dead_end`
- Worker CLI 通过它直接读写黑板，不依赖外部调度器间接写入

**实现总结**:
- `backend/app/solver/muteki/skill/blackboard.py` — 736 行的依赖零 `sqlite3` CLI，支持 read-facts/write-fact/mark-deadend/list-intents/claim 等完整操作
- `backend/app/solver/muteki/skill/SKILL.md` — 面向 Worker 的 skill 定义与使用指引
- `backend/app/solver/muteki/cli_driver.py` — `run_skill` 方法供 Worker 调用
- Worker 提示词模板中已包含黑板技能的使用引导

**验证**: 代码已存在且完整，README 中已标注 ✅ 已修复

---

## 2. Insight Bus（Worker 间实时通信）

**状态**: ✅ 已完成

**目标**:
- 实现 Worker 之间通过事件总线共享已验证事实和死路
- Worker 主动推送/订阅机制，不等待 Coordinator 轮询
- 实现"异构盲区不重叠"

**实现总结**:
- `backend/app/solver/muteki/insight_bus.py` — 127 行，基于 `asyncio.Queue` 的发布/订阅总线，支持 FACT/DEAD_END/FLAG/GUIDANCE 等消息类型
- `backend/app/solver/muteki/runtime/muteki_runtime.py` — 在 `_build_graph` 中创建 `InsightBus`，Worker 通过 `fact()`/`dead_end()`/`flag_found()` 发布
- 订阅/取消订阅由 Coordinator 管理，每个 Worker 拥有独立 inbox
- 发布者不接收自己的消息，新订阅者自动收到历史回放

**验证**: 代码已存在且完整，README 中已标注 ✅ 已修复

---

## 修复完成标准

每项修复完成后：
1. 在本文件对应项标记 ✅ 已完成
2. 在 README.md 对应缺陷描述后标注 `（已修复）`
3. 更新相关文档（architecture.md, multi-agent-core.md）
4. 编写测试用例验证
5. 提交 commit，标注修复内容

---

## 时间线

- 2025-01-XX: 开始 P0 修复
- 待定: 完成 muteki-blackboard skill
- 待定: 完成 Insight Bus

## P1 修复进度

### 3. CAS 内容寻址存储工作区
**状态**: ⏸️ 待开始

### 4. Hypothesis 假设驱动机制
**状态**: ✅ 已完成（代码已存在 — `graph.split_branch` / `resolve_branch` / `branches` 表，通过 `branch_id` 提供假设生命周期管理）

### 5. Context 压缩 / 黑板摘要
**状态**: ✅ 已完成
- `backend/app/solver/muteki/summarizer.py` — 新模块，提供 `compress_facts`、`compress_dead_ends`、`compress_intents`、`build_compressed_context`
- 实现策略：保留已验证的事实优先，候选事实次之，超出限制的聚合为总数摘要
- 死路和意图同样支持分页压缩
- 证据引用截断到 `_MAX_EVIDENCE_REFS` 条

### 6. Worker 路线（Lane）分配与锁定
**状态**: ✅ 已完成（代码已存在 — `_RACE_LANES`、`route_hash`、`branch_id`、`semantic_dispatch.py` 的 `try_claim_activity`、`is_route_suppressed`、`graph.suppress_route`/`reopen_route`/`routes`）

### 10. Learning / Distill 自学习
**状态**: ✅ 已完成
- 上游核心 `backend/muteki/learning/distill.py` 已有完整实现
- `MutekiRuntime` 新增 `_distill()`，求解成功后自动蒸馏模板
- 新增 `backend/app/services/retrieve.py`，提供 `TemplateRetriever` 召回相似模板
- 模板存到 `data/knowledge/`
