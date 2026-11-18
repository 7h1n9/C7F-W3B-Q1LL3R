# 度量纪律护栏（metrics discipline）

日期：2026-11-18
代码：`backend/app/services/metrics_guard.py`、`scripts/assert_feature_live.py`

本项目三次因为统计口径做错决策：功能没被生产路径调用却照旧统计；没确认代码跑过就分析两小时；4/122 vs 127/142 的选择偏差被当成因果。这三件事靠"记得检查"重复发生，所以改成代码检查。

## 五项能力

| 能力 | 入口 | 判据 |
| --- | --- | --- |
| 可溯源快照 | `snapshot(...)` / `append_snapshot(...)` | 每次查询落一行 JSONL：`query_id`、`sql_or_source`、`filters`、`code_mtime`、`data_window`、`computed_at`、`result`；缺 `sql_or_source` 或 `code_mtime` 直接 `ValueError` |
| 功能真跑过 | `assert_feature_live(...)` / `judge_feature_live(...)` | `max(event_time) > code_mtime` 才算 LIVE；否则 `FeatureNeverRan`，并给出"代码比最后一个事件新 N 小时" |
| 选择偏差护栏 | `format_comparison(...)` | 强制打印两组分子/分母/率/分母比；分母比 >3x、任一分母 <10、分子比 >3x、率差 ≥50pp 任一命中即加警告，不可关闭 |
| 预注册校验 | `check_preregistration(...)` | `metrics.primary.name`、`metrics.instrumentation.name`、`analysis.decision_rule` 任一缺失即拒绝出数 |
| 事件覆盖率 | `coverage_report(...)` | 声明的事件类型实际计数为 0 时标 `ZERO-0事件` |

## 命令行

```bash
python scripts/assert_feature_live.py \
  --event-type exploit.first_action \
  --code backend/app/solver/muteki/coordinator.py
```

退出码即结论：`0=LIVE`、`1=NEVER_RAN`、`2=UNKNOWN（无数据源）`、`3=用法错误`。`UNKNOWN` 不能当通过用——断库时的空结果被读成"0%"正是要防的事。每次调用会把本次判定写成快照（`--snapshot-log ""` 可关）。

## 当前边界（重要）

- **快照已接入两个脚本，其余仍未接。** `scripts/analyze_truncation_points.py` 与 `scripts/export_training_transitions.py` 每次出数都会落一行快照（经 `scripts/_metrics_boot.py`）。`assert_feature_live` 仍只被 CLI（`scripts/assert_feature_live.py` 与 `scripts/event_coverage.py --liveness`）调用，没有任何出数脚本把它当入口前置条件。`format_comparison`、`check_preregistration`、`coverage_report` 各接了一个脚本（见文末「剩余三项的接入」），但这只覆盖了这三个脚本，不是全部出数路径。
- `code_mtime` 在 checkout / 复制 / 容器挂载后会变，跨机器比对应改用 commit sha 或镜像 digest。
- JSONL 快照无并发锁，多进程同时写会交错；单进程顺序写安全。
- `judge_feature_live` 把 naive 时间按 UTC 解释，DB 时区若不是 UTC 会有固定偏移。
- 覆盖率只看"有没有事件"，不判断事件内容是否合理。

## 接入记录（2026-11-18 晚）

`scripts/_metrics_boot.py` 提供一行入口：

```python
record_analysis(
    "analyze_truncation_points",              # 写进 query_id 前缀
    "transitions jsonl: <path> | 行数限制: 无",  # 必填，为空即 ValueError
    {"unsolved": {...}, "solved": {...}},     # 本次汇报的数字
    filters={"window": 10, "tail": 3},
    data_window="<src> 全量（43850 行 / 282 runs）",
)
```

三条刻意的不对称：

- `sql_or_source` 为空 → **抛 `ValueError`**。这是脚本的 bug，静默记一行无法重算的数据等于没有记录。
- 写盘失败（`OSError`）→ 向 stderr 打一行警告后**继续**，出数不中断。证据丢了，数字没丢，警告里同时写明这两点。
- 默认日志锚定仓库根 `data/logs/metrics-snapshots.jsonl`（绝对路径），不跟当前工作目录走；否则从 `backend/` 启动会悄悄写进 `backend/data/logs/`。

已接入并实测：

| 脚本 | 记录时机 | `result` 关键字段 |
| --- | --- | --- |
| `analyze_truncation_points.py` | `main()` 末尾，统计全部打印完之后 | `runs`、`transitions`、`status_histogram`、`unsolved`/`solved`（n、median_commands、fact_growth_tail、all_recon_tail、exploit_tail、looping_tail）、`quadrant`、`stuck_fact0_recon`、`advancing` |
| `export_training_transitions.py` | manifest 打印之后（`--dry-run` 也记，`filters.dry_run` 标明没落盘） | `runs_total`、`runs_included`、`runs_excluded`、`transitions`、`split_histogram`、`exclude_reasons`、`redactions_total` |

仍未接入的出数脚本（仍会在无快照的情况下打印数字）：`analyze_first_action.py`、`analyse_direction_lock.py`、`analyse_response_yield.py`、`measure_first_exploit.py`、`measure_vulhub_coverage.py`、`audit_flag_provenance.py`、`eval_signal_index.py`、`prepare_fa_causal.py`、`build_technique_evidence.py`、`run_knowledge_experiment.py`、`mine_box_facts.py`、`credit_verified_solves.py`、`classify_unknown_commands.py`、`dump_tool_responses.py`、`extract_solved_payloads.py`、`extract_vulhub_payloads.py`。

注意：快照只回答"这条数字从哪来"，不回答"两组是否可比"。`format_comparison` 至今没有调用方，`analyze_truncation_points` 的未解/已解对比（175 vs 107）仍是以结局分组——那正是 4/122 vs 127/142 同型的口径，下一步应当接的是它。

## 剩余三项的接入（2026-11-18 晚，第二段）

上一节末尾写的「`format_comparison` 与 `coverage_report` 没有任何脚本调用」在这一段之后不再成立。改了三处，都实测过；下面同时写明**没有**变的部分。

### 1. `format_comparison` → `scripts/analyze_truncation_points.py`

口径：末 `window` 条命令里 `delta_facts` 之和 >0 的 Run 数 / 该组 Run 数，未解组 147/175 vs 已解组 100/107。

实测输出（2026-11-18，data/training/transitions-20261103T161417Z.jsonl）：

```
[组间对比] 口径：末10条命令有 fact 增量的 Run 占比，按 final_status 分组
  分组分母（原样，不合并、不重采样）：未解=175，已解=107
[对比] 未解/引擎失败 vs 已解（对照）
  未解/引擎失败: 147/175 = 84.00%
  已解（对照） : 100/107 = 93.46%
  两组样本量(分母): 175 vs 107，比值 1.64x
  两组命中数(分子): 147 vs 100，比值 1.47x
  率差: 9.5 pp

   未触发选择偏差护栏（阈值：分母比 3x、最小分母 10、分子比 3x）——这只说明数字没有明显失衡，不证明分组无偏

  causal_claim_allowed = True（warnings=0 条）
```

**这条对比不触发任何阈值**：分母比 1.64x < 3x、最小分母 107 ≥ 10、分子比 1.47x < 3x、率差 9.5pp < 50pp。`causal_claim_allowed=True`，但**这不等于可以因果解读**——分组变量 `final_status` 就是结局本身。所以脚本在护栏之外另打一行：

```
  !! 该对比是『按结局分组』：分组变量 final_status 就是结局本身。
     护栏不报警只说明数字没有明显失衡，不证明分组无偏；
     因此本段任何差异都不能解释为『时间预算导致未解』的因果证据。
```

同时脚本会打印「命令数中位 170 vs 54 无法喂进该护栏」，并把这个结构性盲区写进快照的 `result.comparison`。

### 2. `check_preregistration` → `scripts/prepare_fa_causal.py`

闸门在**读 DB / 生成分组之前**，`strict=True`；`--dry-run` 同样走闸门。缺项时：

```
[预注册检查] fa-causal-v1 (.probe/.tmp-fa-causal-missing-rule.yaml): REFUSED
!! 缺少必需项: analysis.decision_rule
FAIL  预注册校验未通过，拒绝生成分组（未触碰数据库、未写任何文件）
```

逃逸开关 `--skip-preregistration` 会打 4 行警告，并把 `preregistration.verified=false` 写进分组 JSON。

顺带修了两个会静默出错的点：

- **`fa_seen` 恒为 False。** 原 SQL 查 `event_type='exploit.first_action'`，而真实事件名带 `muteki.` 前缀（`adapter/event_bridge.py` 统一加），永远匹配 0 行。结果是「排除已有 FA 历史的题」这条规则从未生效——已锁定的正式分组里 `excluded_for_prior_fa=0` 就是这个 bug 的证据。
- **`--dry-run` 覆盖正式分组。** 原来 dry-run 也写 `experiments/fa-causal-v1.grouping.json`，一次自测就把锁定名单换成了合成样本。现在 dry-run 默认写 `…grouping.dryrun.json`。

### 3. `coverage_report` → `scripts/event_coverage.py`（新文件）

```bash
python scripts/event_coverage.py                                    # 四个默认事件
python scripts/event_coverage.py --expect muteki.worker_step=100000  # 精确期望
python scripts/event_coverage.py --liveness backend/app/solver/muteki/coordinator.py=muteki.exploit.first_action
python scripts/event_coverage.py --db-port 3399                      # 无数据源分支
```

退出码：`0=达标`、`1=有 0 事件或低于期望`、`2=无数据源（无法判定）`、`3=用法错误`。

两类必须区分的「没有」：

| 现象 | 含义 | 输出 |
| --- | --- | --- |
| 表里有这个 event_type，count=0 | 埋点在，但功能没产出 | `!! ZERO-0事件` |
| 表里查不到这个 event_type | 埋点名写错（少了 `muteki.`） | `表中未出现（不是 0，是没这个 event_type）` |
| 连不上 DB | 没测量，不是 0 | `无法判定（无数据源）`，且**不渲染任何计数表** |

### `assert_feature_live` 的接入点（尚未接入，只是清单）

规则：凡是「某功能产出了多少 / 该功能有效率多少」的脚本，第一行出数前都应先过 `assert_feature_live(code_path=发射该事件的代码)`。按当前脚本与事件的对应关系：

| 脚本 | 应先检查的事件 | `code_path` |
| --- | --- | --- |
| `measure_first_exploit.py`、`analyse_response_yield.py`、`analyse_direction_lock.py`、`export_training_transitions.py` | `muteki.worker_command` | `backend/app/solver/muteki/container_exec.py` |
| `analyse_direction_lock.py`、`mine_box_facts.py` | `muteki.fact_added` | `backend/app/solver/muteki/adapter/official_observation.py` |
| `prepare_fa_causal.py`（FA 历史过滤）、`event_coverage.py --liveness` | `muteki.exploit.first_action` | `backend/app/solver/muteki/coordinator.py` |
| `measure_vulhub_coverage.py`、`build_learned_playbook.py`、`eval_signal_index.py` | 不依赖运行事件（读题库/离线索引），**不适用** | — |

`event_coverage.py --liveness` 是这条规则的示范实现：覆盖率回答「有没有产出」，liveness 回答「当前这版代码有没有跑过」，两者不可互相替代（有可能有 5 条历史事件，但代码改完之后一条都没有）。

### 这一段之后仍然没接护栏的出数脚本

`scripts/` 下会打印数字但不经 `record_analysis` / `assert_feature_live` 的文件（与上一节名单一致，未新增覆盖）：`analyze_first_action.py`、`analyse_direction_lock.py`、`analyse_response_yield.py`、`measure_first_exploit.py`、`measure_vulhub_coverage.py`、`audit_flag_provenance.py`、`eval_signal_index.py`、`prepare_fa_causal.py`、`build_technique_evidence.py`、`run_knowledge_experiment.py`、`mine_box_facts.py`、`credit_verified_solves.py`、`classify_unknown_commands.py`、`dump_tool_responses.py`、`extract_solved_payloads.py`、`extract_vulhub_payloads.py`。

其中 `prepare_fa_causal.py` 现在接了预注册闸门，但**仍未接** `record_analysis` 快照——所以它能拒绝出数，却不会留下「这次分组是哪版代码生成的」的记录。
