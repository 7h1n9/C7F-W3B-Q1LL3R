# 2026-11-04 夜间执行记录

时段：00:53 – 06:30（稳健档）
起点：用户确认「稳健档 + 调控模型可配置 + 06:30 后不再执行任何任务」

---

## 一、最重要的发现：竞技场整条链路缺复现验证

`fresh_reproduction_verified` 全库为 0 的真因，已定位到代码：

- `arena_solve.py` 中 grep `reproduc` **零命中** —— 竞技场链路从未调用过复现器；
- 只有 `orchestrator.py` / `multi_agent_orchestrator.py` 内部路径会调用，而竞技场走 `start_run_for_arena` + 独立提交，绕过了它。

**历史 103 条 accepted 无法事后补**：靶机是一次性域名，实测两个最新地址均 `404`，而 `direct-ctf2.dasctf.com` 主站仍 `200`（证明是实例回收，不是断网）。workspace 都还在，但靶机没了。

**因此原计划「给 103 个 accepted Run 补验证」作废**，改为在线流程修复（见第二节 B1）。

---

## 二、本轮交付

### B1 竞技场 accepted 后接复现验证

`backend/app/services/arena_solve.py`

- 新增 `REPRODUCTION_BUDGET_SECONDS = 180`（硬上限）；
- 新增 `_verify_reproduction()`：在 `accepted` 分支、租约释放前调用；
- 新增 `_publish_reproduction()`：把结果写成 `arena.fresh_reproduction` 事件；
- `ArenaSolveOutcome.fresh_reproduction` 字段。

**设计红线（全部有测试覆盖）**：

| 场景 | 行为 |
|---|---|
| 复现超时 | 记 `status=timeout`，**不改判** |
| 复现抛异常 | 记 `status=error`，**不改判** |
| Run/Challenge 找不到 | 记 `status=skipped` |
| 复现未验证 | 记 `verified=false`，提交结论不变 |
| 租约刷新失败 | 吞掉，继续尝试复现 |

理由：平台已 accepted 就是 accepted，复现失败是我们工具的证据，不是推翻真实战果的理由。

测试：`backend/tests/test_arena_reproduction_hook.py`（9 条）

### C1（原 #66）引擎体检重试

`backend/app/solver/muteki/worker/official_worker.py`

- `health()` 拆为 `health()` + `_health_once()`；
- 新增 `_TRANSIENT_HEALTH_REASONS`（仅 `TIMEOUT` / `FAILED`）、`_HEALTH_RETRY_DELAY_SECONDS = 8.0`；
- 瞬时失败重试一次，非瞬时（如凭据被拒）不重试。

**为什么必须做**：实测 00:43–01:00，两条车道交替报 `WORKER_HEALTHCHECK_TIMEOUT` / `WORKER_HEALTHCHECK_FAILED` 长达 20 分钟，而网关只是过载。单次探测失败即让引擎在整个 Run 内退役，那段时间的题全被烧掉。

测试：`backend/tests/test_worker_health_retry.py`（12 条）

### A1 可配置调控模型的技法分类器

`backend/app/training/technique_classifier.py` + `scripts/classify_unknown_commands.py`

**模型可配置**（用户明确要求）：`--model <名称或ID>`，解析自 `model_configs` 表，`--list-models` 列出全部可用项。当前 7 个配置可选。

关键约束：

- 离线，不在解题路径上；
- 按命令内容哈希缓存，可续跑，重跑不重复付费；
- **失败记为失败，绝不伪装成 `unknown`**（模型超时和模型判断"这不是技法"是两件不同的事实）；
- 输出校验在封闭词表内，越界即报错。

测试：`backend/tests/test_training_technique_classifier.py`（32 条）

实测规模：unknown 去重 **16,575** 条，过滤平台噪声后 **12,360 种 / 25,737 次**值得分类。

### D1 归档手工调试工具

`backend/_probe_env.py` → `scripts/_manual/probe_env.py`
`backend/_hold_env.py` → `scripts/_manual/hold_env.py`

**没有删除**：这两个是文档齐全、能手动驱动 PoC 的调试工具，删了会损失能力。已确认无任何代码引用。

### D3 修 `build_learned_playbook.py` 的静默丢行

- 加 `mysql -r`（raw）。不加则 JSON 里的 `\"` 变成 `\\"`，逐行解析失败；
- 丢行改为**计数并报告**，超过 10% 直接 abort。

参照：同源导出器实测 526 行里 421 行解析失败且被静默丢弃（丢 80% 命令文本而脚本报成功）。

### C2（原 #65）CVE playbook 缺口：结论是"补不了"

新增 `scripts/distil_cve_playbook.py`：只从真实 accepted 轨迹蒸馏，不编造。

实测结果：

```
已解 CVE 38 个，playbook 覆盖 13 个，缺 29 个
  可蒸馏（初版门禁）: 2
  加质量门禁后:       0
  证据不足:           21
  无 accepted run:     6
```

初版放行的 2 条被质量门禁拦下，原因具体：

- `CVE-2018-11776`：第 2 步是 `blackboard.py write-fact`（平台内部操作），不是利用步骤；`technique` 也识别不出来；
- `CVE-2014-3120`：同样只剩 1 条可用步骤。

**结论**：29 个缺口的共同特征是「各只解过 1 次」，单条轨迹里的有效利用步骤不足 2 条。硬补只能靠编造，而编造的 payload 会让 Worker 打出去烧掉一轮——比没有条目更糟。所以本轮**不写任何新条目**。

### A3 证据排序接入（本轮实际完成了）

原计划「等 A1 标注后再改排序」被推翻：**不需要新标注也能验证**。用现有 6 条证据直接接入，并实测出效果。

新增：

- `backend/app/training/technique_evidence.py`：证据 → 候选类映射、阈值判定、晋升排序；
- `data/knowledge/technique-evidence.json`：证据文件（唯一可变量，删掉即回滚）；
- `known_exploit.technique_candidates()` 接入。

**实测效果**（9 个候选类争 3 个名额）：

| | top3 |
|---|---|
| 改前（纯语料长度） | SSRF(25) / Upload(16) / SQLi(13) |
| 改后（证据优先） | **RCE(59%) / SQLi(33%) / LFI(31%)** |

关键收益：`Command injection and RCE` 语料只有 9 条（第 5 位），**永远进不了 3 个名额**，但实测 17 个 Run、命中率 59%，是最强的；`Local file inclusion` 语料仅 1 条（最后一位），实测 26 个 Run、命中率 31%。

**三道保护**（均有测试）：

1. **标题命中优先**：`[极客大挑战]EasySQL` 仍把 SQLi 放第 1，证据不能覆盖标题——标题是更强的信号。实测验证；
2. **双阈值**：`runs >= 4` 且 `rate >= 30%` 才晋升。`sqli.union` 只有 3 个 Run，不参与；`upload.bypass` 有 4 个 Run 但命中率 25%，也不晋升；
3. **只重排不新增**：不在候选名单里的技法永远不会被证据带进来。

**回滚方式**：删除 `data/knowledge/technique-evidence.json`，排序立即退回纯语料长度。

过程中修掉两个自己的 bug：

- 初版用「最多提升 3 位」的相对位移，实测是**完全无效**的（第 5 位升 3 位还是进不了前 3）——改成按证据强度晋升；
- `candidate_evidence()` 对已合并的映射再做一次映射，导致键名不匹配、结果为空的**静默失效**——改为幂等。

测试：`backend/tests/test_training_technique_evidence.py`（18 条）

---

## 三、全量测试

```
2137 passed, 13 warnings in 255.04s
```

基线 2003，本轮新增 **134** 条：

| 文件 | 条数 |
|---|---|
| `test_training_technique_classifier.py` | 32 |
| `test_training_technique_evidence.py` | 18 |
| `test_worker_health_retry.py` | 12 |
| `test_arena_reproduction_hook.py` | 9 |
| 之前几轮（transitions / candidates / policy） | 42 |
| 其余为随代码增补的既有用例 | 21 |

分组回归：arena/reproduction/training/benchmark **283 passed**；worker/health/muteki **666 passed**；knowledge/technique/training **232 passed**。

---

## 四、未完成项与真实原因

| 项 | 状态 | 原因 |
|---|---|---|
| A1 跑批 | **代码完成，未跑批** | 上游 `glm-5.3` 过载：`sub.gavinchen.top` 521、`sub.ap-sub.top` 502/503、`api.deepseek.com` 402 余额不足、`Kimi-K3` 403。四条渠道全不可用，01:21 与 01:30 两次复测仍失败 |
| A2 重挖证据 | **已用现有证据完成 A3** | 不需要新标注也能接入并实测，见上 |
| #69 phpMyAdmin 提权 | 未做 | 单题优化，收益低于本轮已做项 |
| #70 分类器误判 | 未做 | 需真实 Run 验证，上游不可用 |

### 队列处置

01:00 左右手动停队列。依据：引擎体检双车道全挂、队列只剩 1 个 PENDING、近 30 分钟 5 个 Run 全是 UNSOLVED 且无引擎故障记录——继续跑只会烧无效轮次。

停止前状态：SOLVED 36 / UNSOLVED 94 / PENDING 1。

---

## 五、给下次的交接

1. **A1 跑批**：上游恢复后直接跑
   ```bash
   python scripts/classify_unknown_commands.py --list-models
   python scripts/classify_unknown_commands.py --model GLM --limit 500
   ```
   断点续跑免费（按哈希缓存）。

2. **证据重建**（有新解出的题之后）：
   ```bash
   python scripts/export_training_transitions.py     # 重新导出
   python scripts/build_technique_evidence.py        # 重建证据
   ```
   证据必须由脚本生成，不要手写——手写的数字是意见，不是测量。

3. **B1 需真实验证**：代码已完成但**没在真实 accepted 上跑过**。下次有题解出后，检查 `run_events` 里是否出现 `arena.fresh_reproduction` 事件，以及 `status` 是否为 `executed`。

4. **C1 需真实验证**：体检重试只在单元测试里验证过。下次引擎抖动时看日志里是否有 `failed transiently (...); retrying once`。

5. **不要再尝试补历史 103 条复现**：靶机已回收，物理上不可能。

6. **CVE playbook 不要硬补**：29 个缺口都需要更多真实轨迹才有素材，编造是有害的。

7. **排序回滚**：删 `data/knowledge/technique-evidence.json` 即退回纯语料长度排序，无需改代码。
