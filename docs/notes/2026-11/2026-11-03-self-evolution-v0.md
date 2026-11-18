# 自进化 v0.1：从历史轨迹到策略版本

日期：2026-11-03
状态：数据层已落地；策略层候选与 A/B 未开始
范围：仅策略层自进化；**不涉及模型权重微调**

---

## 1. 结论先行

1. **"把 agent 丢进 CTF-Dojo 就能自进化"不成立。** CTF-Dojo 提供的是可执行环境 + 可验证奖励 + 轨迹采集，缺的正是"用轨迹改进 agent"的那一层。它对我们有价值，但属于第 4 阶段，且必须先补数据质量缺口。
2. **本项目的瓶颈不是知识库，是信号。** 全历史 44,910 条 `worker_command`，只产出 21,326 条 fact / 1,710 条 dead_end；每 100 条命令仅 2.7 个 fact。在这种稀疏度上直接训权重等于拟合噪声。
3. **先做策略层闭环**：把历史变成可切分、可审计、可回滚的 transition 数据集，在 dev split 上做 candidate vs baseline 的配对 A/B，只有通过评测门禁的候选才允许进入生产 playbook。
4. **DeepSeek V4.1 当前不可用**（见第 7 节），本轮开发任务由当前会话模型执行。

---

## 2. 本次落地（已提交代码）

| 文件 | 作用 |
| --- | --- |
| `backend/app/training/transitions.py` | 纯函数核心：事件 → `(state, action, observation, label)`；质量门禁；三级脱敏；按 CVE / 题目分组切分 |
| `backend/app/training/policy.py` | `policy_version` 指纹：把策略源码与知识语料哈希成一个短 id |
| `scripts/export_training_transitions.py` | 只读 MySQL 导出器，产出 `transitions-*.jsonl` / `runs-*.jsonl` / `manifest-*.json` |
| `backend/app/training/candidates.py` | P1 候选挖掘：形状级死路 + 技法级证据（有效/死路） |
| `scripts/build_policy_candidates.py` | 读导出文件 → 写 `data/knowledge/policy-candidates/`，不碰 DB 与 arena |
| `backend/app/benchmark/experiment.py` | manifest 新增 `policy` 段；resume 时策略版本变化会告警（不致命） |
| `backend/app/services/learned_skills.py` | 修 `SECRET_KV` 交替顺序：`Bearer <token>` 之前会把 "Bearer" 当密钥值、把真 token 留在明文 |
| `backend/tests/test_training_transitions.py` | 21 条用例 |
| `backend/tests/test_training_candidates.py` | 13 条用例 |
| `backend/tests/test_training_policy.py` | 6 条用例 |

### 实测数据（2026-11-03 全量导出）

```
runs_total              633
runs_included           282      (train 224 / dev 23 / test 35)
runs_excluded           351      no_commands 271 / status 79 / infrastructure 1
transitions          43,850
accepted_runs_included  103
fresh_repro_runs        0   ← 关键缺口
redactions_total     90,708
known_secrets_redacted  121   (= arena 全部 accepted flag)
accepted flag 泄漏复检    0   (121 条原文逐条 grep)
```

### 过程中修掉的三个真问题

1. **mysql 批量模式改写了反斜杠。** `-B` 会把 JSON 文本里的 `\"` 输出成 `\\"`，导致逐行 `json.loads` 失败、**421/526 行被静默丢弃**（旧 `build_learned_playbook.py` 有同样的静默丢弃）。修法：`mysql -N -B -r`。
2. **截断在脱敏之前。** 600 字符切在 flag 中间，半截 flag 既匹配不到精确串也匹配不到 `{...}` 正则，实测泄漏 `flag{Y0u_@re_3enset1ve_...`。修法：先脱敏再截断 + 未闭合头部规则。
3. **平台 flag 前缀不止 `flag{`。** 共享 sanitizer 只认 `flag{`，而 arena 实际有 `DASCTF{` 等。修法：导出时把 121 条 accepted flag 原文当字面量精确替换。

---

## 3. 为什么先做策略层

现有可复用的评测承载层已经齐全：

- `ExperimentRunner`：不可变 manifest、resume、原子写、`MIN_PAIRED_SAMPLES=10` 门禁；
- `evaluate_run` / `aggregate_pass_at_k`：与 CTF-Dojo 同口径的 Pass@1；
- `run_process_metrics`：`repeat_attempts`、`prior_misled`，可当适应度惩罚项；
- `learned_skills`：候选 → 人工审核 → 发布 → 失败回滚，链路已存在。

缺的只是"把轨迹变成候选策略"的那一步。相反，权重微调需要的样本量、可复现环境、许可证，现阶段一条都不满足。

---

## 4. 分阶段计划

### P0 数据契约（本次完成）

- 交付：transition schema、导出器、脱敏、切分、`policy_version`。
- 门禁：`accepted flag 泄漏 == 0`；`pytest tests/test_training_transitions.py tests/test_training_policy.py` 全绿。
- 回滚：`data/training/` 整目录可删，不影响运行时。

### P1 候选策略生成（已跑到"粒度结论"，产物已有）

- 输入：`train` split 的 transitions。
- 产出：`data/knowledge/policy-candidates/candidates-<stamp>.json`，三类增量：
  - `dead_ends`（Reflexion 式，形状级）：同 (tool, 形状) 在 ≥2 个不同 Run 撞墙且从未产出 fact；
  - `techniques_effective`：技法在 ≥2 个 **accepted** Run 里带来 fact 增量；
  - `techniques_dead`：技法在 ≥2 个 Run 撞墙，且**任何** Run（含未解）都从未产出 fact。
- 硬约束：
  - 每条候选必须带 `source_run_ids`，不得编造；
  - 候选文件不得含 flag / 内网地址（复用同一套 `redact_public_text`）；
  - 候选只写 staging 目录，**不直接改 `strategy.py`**——改代码无法按实验回滚。

**实测（2026-11-03，43,850 条 transition / 103 个 accepted Run）：**

| 粒度 | 结果 | 判定 |
| --- | --- | --- |
| 精确命令序列（Voyager 式） | 99 条候选，**0 条跨 Run 佐证** | 弃用：不同 Run 的同一技法写法必然不同 |
| 技法序列 | 0 条候选 | 弃用：83% 命令无词表命中，且单条命令常捆绑多技法 |
| **技法证据（当前采用）** | **6 条有效技法（背后 23 个 Run）、0 条死技法** | 采用 |
| 形状级死路 | 203 个形状、仅 1 个跨 Run 复现且该形状产出过 fact → **0 条** | 保留规则，等 dead_end 信号变多 |

有效技法实测（`runs_fact` / `runs_dead`）：`rce.ognl` 10/6、`lfi.traversal` 5/9、`recon.source` 3/7、`lfi.wrapper` 3/6、`sqli.boolean` 2/4、`sqli.union` 2/3。

**死技法为 0 本身是结论**：现有数据里没有任何技法"只撞墙、从不产出 fact"。初版曾把 `recon.dir`（ffuf/gobuster）判死——因为只拿 accepted Run 当正向证据；修正为"任何 Run 的 fact 增量都算反证"后撤销。这条规则必须保留，否则会禁掉侦察本身。

- 门禁：候选在 `dev` split 上可被 `KnowledgeRetriever` 检索到（下一步）。
- 验收：`runs-*.jsonl` 里 `exclude_reason` 分布不变；候选数可解释（不追求数量）。

### P2 配对 A/B（P1 之后）

- 命令：`python -m app.benchmark --arms off,on --repeats N --output-dir <dir>`，`--resume` 可续。
- 判据（缺一不可）：
  1. 配对样本 ≥ `MIN_PAIRED_SAMPLES`（10），否则只能 `descriptive_only`；
  2. Pass@1 提升方向一致；
  3. `repeat_attempts` 不上升（防止"多打几枪蒙中"）；
  4. `prior_misled` 不上升（防止知识把人带进沟里）；
  5. manifest 的 `policy_version` 记录在案，跨版本结果不得混算。
- 回滚：删除候选文件 + 重跑实验；manifest 不可变，历史结论保留。

### P3 发布与新鲜度

- 只有 P2 全绿才走 `learned_skills.publish()`。
- 发布后 `test` split 不复用，只用于最终一次性评估。

### P4 CTF-Dojo 环境（后置，需先满足前置条件）

- 前置：`fresh_reproduction_verified` 覆盖率 ≥ 30%（当前 0/631）。这一列是"成功轨迹真的可复现"的唯一证据；为 0 意味着**我们没有任何一条轨迹能证明是稳定解法**，此时拿它当正样本训练只会学到运气。
- 定位：用 CTF-Dojo 的 658 个 Docker 化挑战补环境多样性 + 可验证 reward；轨迹采集仍走 EnIGMA+ 口径。
- 许可：CTF-Dojo 为 **CC-BY-NC-4.0**，毕设引用需注明且不得商用。

---

## 5. 已知缺口（按优先级）

| 级别 | 缺口 | 影响 | 处置 |
| --- | --- | --- | --- |
| P0 | `fresh_reproduction_verified` 全库为 0 | 正样本无"可复现"背书 | 对 103 个 accepted run 批量跑 `FreshReproductionExecutor` |
| P0 | 训练信号稀疏（2.7 fact / 100 命令） | 策略候选几乎无正向样本 | 先修 fact 通道召回，再谈训练 |
| P1 | `group_key` 只用 CVE 或 challenge id | 同产品不同 CVE 仍可能跨 split | 增加 product+technique 轴 |
| P1 | 技法词表只覆盖 17% 命令（5,301/33,914 有标签） | 有效/死技法判定样本偏小（6 条） | 扩词表要按"是否改变动作"审，别堆正则 |
| P1 | `SensitiveValueSanitizer.SECRET_KV` 会把 `username=` 的值当密钥 | SQLi payload 的 `username=admin' or '1'='1` 被削 | 训练侧暂接受；发布侧需复核 |
| P1 | 351/631 被排除，其中 271 条是 0 命令 | 历史一半不可用 | 属既有事实（早期无 worker 命令），不需修 |
| P2 | `FLAG_ANY_RE` 会把 `grep "flag{" .` 误伤 | 命令可读性轻微损失 | 已用"6 字符以上实体"规则限缩，接受 |

---

## 6. 复现命令

```bash
cd backend
env -u PYTHONPATH ../.venv3.11/Scripts/python.exe -m pytest \
  tests/test_training_transitions.py tests/test_training_policy.py -q

cd ..
./.venv3.11/Scripts/python.exe scripts/export_training_transitions.py --dry-run
./.venv3.11/Scripts/python.exe scripts/export_training_transitions.py
# 泄漏复检：用 arena_submissions 的 121 条 accepted flag 原文逐条 grep 导出文件，必须为 0
```

---

## 7. DeepSeek V4.1 验证结论（阻塞）

2026-11-03 实测三条通道：

| 通道 | 结果 |
| --- | --- |
| `api.deepseek.com`（官方，配置 `deepseek-v4-flash`） | `/v1/models` 仅返回 `deepseek-flash`、`deepseek-v4-pro`；**没有 v4.1**。且实际调用返回 **402 Payment Required**（`MODEL_QUOTA_EXCEEDED`），账号余额不足 |
| `sub.gavinchen.top`（配置 `claude-deep`, model `deepseek-v4.1-flash`） | 模型测试返回 `Model "deepseek-v4.1-flash" is not available for this group`；`/v1/models` 返回 **HTTP 521**（源站不可达） |
| `sub.ap-sub.top`（当前 glm 车道所用） | `/v1/models` 仅 `glm-5.3`、`glm-5.3-flash` |

结论：**没有任何可用通道提供 DeepSeek V4.1**，切换会直接把双车道打空。故本轮不切换，等上游恢复或换个提供 v4.1 的渠道再验证。

---

## 8. 下一步（按顺序）

1. 给 103 个 accepted run 补 `fresh_reproduction_verified`（P0，决定 P1/P2 能不能做）。
2. 把 6 条技法候选接进 `dev` split 的检索门禁：确认 `KnowledgeRetriever` 能召回、且不会把 `lfi.traversal` 这类通用词条塞满 1600 字符的 PRIOR 预算。
3. dev split 上跑 P2 配对 A/B（`--arms off,on`，配对 ≥10 才允许下结论）。
4. 队列空闲时跑全量测试（当前基线 2003 + 本次新增 40 = 2043，待确认）。
