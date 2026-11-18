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

- **快照已接入两个脚本，其余仍未接。** 原文写的"尚无生产调用方"已过时：`scripts/analyze_truncation_points.py` 与 `scripts/export_training_transitions.py` 现在每次出数都会落一行快照（经 `scripts/_metrics_boot.py`）。但 `assert_feature_live` 仍只被 CLI 调用，`format_comparison` 与 `coverage_report` 仍然**没有任何脚本调用**——它们是"可以被调用"的护栏，不是"已经在拦"的护栏；接入点是所有出数脚本的入口（先 `assert_feature_live`，再 `snapshot`，最后 `format_comparison`）。
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
