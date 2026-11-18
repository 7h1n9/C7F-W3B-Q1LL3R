# 自动化执行记录：Muteki 竞技场夜间跑批收尾（2026-10-27）

## 2026-10-27 06:35 首次执行（结论部分有误）
- 队列已自行停止，无 RUNNING/PAUSED_RECOVERY 残留。
- 12 条 Run：SOLVED 3 / UNSOLVED 8 / FAILED_ENGINE 1；双 lane 75%、单 lane 25%。
- 当时误判「窗口内 12 条 Run 零评分」，产出 v1 报告。

## 2026-10-27 09:25 复核执行（定稿）
- API 已下线（8000 拒绝连接），以 DB + `.probe/_q_final.json` + `arena_watch.log` 为准；队列确认停稳，无需 stop/cancel。
- **修正 v1 结论**：22:42–23:06 UTC 存在跑后补算，窗口内 4 条 Run 有评分且**全部 COMPLETED**（非 NO_PREDICTION）；23:06 后全库约 80 条 NO_PREDICTION 转 COMPLETED。
- **P0 定位**：8 条 UNSOLVED Run 评分行完全缺失，根因 MySQL `1205 Lock wait timeout`，3 次重试耗尽后放弃（`solver_scoring.py:589`）。历史 200 条 UNSOLVED 覆盖率 100%，属回归。
- P0-2 队列 52 条 PENDING 的 run_id 指向已结束 Run（含 2 条已 SOLVED），可追溯至 10-25。
- 验证项：model.selected 延迟 1–2s（12/12）确认修复；predictions 新增 8 条 COMPLETED、0 新增 FAILED（129 条存量未减）。
- 调度 05:15 本地即进入 last-start 停工，05:17 后到 06:30 空转 73 分钟。
- 环境坑补充：`~/.local/bin/env` 空壳 shim 吞参数；须 `export MSYS_NO_PATHCONV=1` + shell 内 unset 代理。
- 产出：`.probe/night_run_report_20261027.md`（v2 定稿，已覆盖 v1）。

## 2026-10-27 22:10 第三次执行（v3 收尾复核，定稿）
- API 已恢复在线（8000 LISTENING），在线+DB 双证：`running=false`、counts 52/3/3、495 条 Run 中 0 条 RUNNING/PAUSED_RECOVERY，未触发 stop/cancel。
- 成绩与 v2 一致：尝试 12 / SOLVED 3 / UNSOLVED 8 / FAILED_ENGINE 1；双 lane 75%、单 lane 25%。
- 关键新事实：评分机制已于当日 09:40 整体下线（`.probe/scoring_removal_20261027.md`，代码净删 1464 行、两表停写不 DROP），故 8 条缺失评分行不再回补，`run_scores` 冻结。
- 新增历史口径 P1：零产出 Run（actual_tokens=0）走 `token_eff=EFFICIENCY_CAP` 特判拿满 40 分，叠加时间项 60 分 = 100 分，未解出也满分（91beec41）。
- 复查：125 行报告追加第 7 节（v3）；P0-2（52 条 PENDING 指向终态 Run）与早停 73 分钟仍未处理。
- 环境修正：`env -u ... curl` 前缀在本机被 `~/.local/bin/env` shim 吞掉会挂死（2.5 分钟无输出）；改用 shell 内 `unset` + `curl --noproxy '*'`，且用管道喂 python 而非 `/tmp` 路径（Windows python 不认 MSYS 临时路径）。

