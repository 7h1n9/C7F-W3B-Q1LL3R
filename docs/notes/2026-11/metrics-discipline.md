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

- **尚无生产调用方。** 这些函数没有被 `backend/app/api/`、报表脚本或实验脚本调用。现在它是"可以被调用"的护栏，不是"已经在拦"的护栏；接入点是所有出数脚本的入口（先 `assert_feature_live`，再 `snapshot`，最后 `format_comparison`）。
- `code_mtime` 在 checkout / 复制 / 容器挂载后会变，跨机器比对应改用 commit sha 或镜像 digest。
- JSONL 快照无并发锁，多进程同时写会交错；单进程顺序写安全。
- `judge_feature_live` 把 naive 时间按 UTC 解释，DB 时区若不是 UTC 会有固定偏移。
- 覆盖率只看"有没有事件"，不判断事件内容是否合理。
