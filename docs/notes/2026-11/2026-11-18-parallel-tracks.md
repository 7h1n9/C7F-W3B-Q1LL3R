# 2026-11-18 并行开发编排（W1）

三条轨道并行，两条轨道（A 运行时实验 / B 决策层）保持串行独占。本文记录**文件所有权**与**合并门**，避免并行下的写冲突与「各自都绿、合并即废」。

## 派发前提（已核实，非假设）

| 前提 | 核实方式 | 结果 |
|---|---|---|
| E 轨道可离线 | `data/training/transitions-20261103T161417Z.jsonl` 68MB；`data/workspaces/*/muteki/evidence/cli-conversations` 566 个 Run | 成立，不需 DB |
| D 轨道确有缺陷 | `global.css:155` 硬裁；`WorkspacePage.tsx:463` 摘要替换原文；`sourceContent` 仅作 `:576` 的 `title=` | 成立 |
| F 轨道无 DB 依赖 | 纯函数 + CLI，DB 缺失时降级 | 成立 |
| 三轨文件不重叠 | 见下表 | 成立 |

## 文件所有权

| 轨道 | 独占写入范围 | 备注 |
|---|---|---|
| **E** 轨迹适配/诊断 | `backend/app/training/trajectory.py`、`backend/app/training/diagnosis.py`、`backend/tests/test_trajectory_compression.py`、`backend/tests/test_diagnosis_layers.py`、可选 `scripts/inspect_trajectory.py` | 全部新建，零冲突 |
| **D** 前端看板 | `frontend/src/pages/WorkspacePage.tsx`、`frontend/src/styles/global.css` | 仅此两文件 |
| **F** 度量护栏 | `backend/app/services/metrics_guard.py`、`backend/tests/test_metrics_guard.py`、`scripts/assert_feature_live.py`、可选 `docs/notes/2026-11/metrics-discipline.md` | 全部新建 |
| 主控（我） | `docs/notes/2026-11/2026-11-18-parallel-tracks.md`、`docs/2026-11-18-plan.md` | 不参与代码轨 |

禁止项（已下达给各轨）：启动 Docker / 后端 / 前端；连 MySQL 3307；改 `backend/muteki/`（vendored 上游）；`git checkout` / `stash` / `commit`；跑全量测试。

## 为什么并行上限是 3

1. **合并门只有一个**：全量 `pytest tests/` 实测 2486 项 / 约 4 分 45 秒。未装 xdist，`test_mysql_*` 连单实例 3307。四轨以上只是排队。
2. **服务只有一份**：8000 / 5173 各一个实例，起服务的人与跑实验的人不能同时干活。
3. **有状态操作必须排他**：`.venv3.11` 装依赖、DB 迁移、`arena/orphans/reclaim` 都不可并发。

## 合并门（按顺序，任一失败则回退对应轨，不得合并）

1. **范围检查**：每轨 `git status --porcelain` 报告的文件必须落在其所有权范围内。越界即拒。
2. **单轨自测**：E 的两个测试文件、F 的测试文件、D 的 `npm run build` 必须各自通过（各轨自报，我复核）。
3. **合并后全量**：`cd backend && env -u PYTHONPATH ../.venv3.11/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`。基线 **2486 passed**；Docker 未起时允许存在 10 个 MySQL 连接失败，但**不允许新增任何非 MySQL 失败**。
4. **到用户手上**（本项目的核心教训，逐轨判据）：
   - E → 抽 5 个真实 Run，压缩后**能答出**最后一条命令、关键地址、注入点（用测试固化，不看肉眼）
   - D → 三处硬编码 grep 消失 + 构建通过。**不得声称「界面已完整显示」**，因为无人开浏览器验证；只能声称「裁切已移除、展开入口已加」
   - F → `assert_feature_live.py` 在无 DB 时明确报「无法判定」，不误报通过

## 未派发（有意）

| 项 | 原因 |
|---|---|
| **G** 30 题指纹标注 | 需 DB 查询 + 需人工判断指纹正确性，我无法替代 |
| **P0** 20 题随机对照 | 运行时任务，串行队列 + 每 agent 1 格环境，不可并行 |
| **P1** 五段探测流程 | 工作量集中在 `coordinator.py`(4179 行) 与 `cli_solver.py`(vendored)，多写者必冲突 |
| **P1** fact 通道改造 | 同上，且是动态预算/研究方向/看板的前置 |
| **P2** 单环境约束 | 需 DB 验证 reclaim 行为 |

## 并行会放大的风险（已知并接受）

「产出≠生效」在并行下会成倍恶化：三条轨道都会报告「我这块绿了」，而单测绿 ≠ 用户能看到。因此合并门的第 4 条是逐轨写死的、可机械验证的判据，而不是「看起来完成了」。

---

## W1 执行结果（2026-11-18 08:45–09:12）

| 轨道 | 新增/修改 | 自测 | 主控独立复核 |
|---|---|---|---|
| E | `trajectory.py` 1305 + `diagnosis.py` 910 + 测试 440/414 + `inspect_trajectory.py` 194 | 67 passed | 67 passed（复跑）；CLI `sweep`/`compress`/`questions` 实跑确认 |
| D | `global.css` 338→360、`WorkspacePage.tsx` 1035→1186 | `npm run build` exit 0 | `npx tsc --noEmit` exit=0 / 0 errors；四处硬编码 grep 归零 |
| F | `metrics_guard.py` 771 + 测试 365 + `assert_feature_live.py` 248 + 说明 34 | 23 passed | 23 passed（复跑） |

**全量门**：`2519 passed / 0 failed`（三轨前）→ `2585 passed / 1 failed`（三轨后）。

唯一失败 `test_muteki_round_deadline_reserve.py::test_the_grace_counts_towards_the_round` 判定为**既有脆弱用例，非本次回归**，依据：
1. 该文件 mtime 2026-10-15，本次未改动；`coordinator.py` mtime 2026-11-09，本次未改动。
2. 单独跑 10 passed；与 E 的两个测试文件同跑 77 passed；单独重复 20 次 0 失败。
3. 机制明确：断言取等号 `seconds_left = ROUND_BUDGET + GRACE`，而 `_seconds_left()` 是 `deadline - monotonic()`，`deadline` 在测试里由 `monotonic() + seconds_left` 生成。两者相差一个 ε，`35 - ε >= 35` 仅在 ε 被浮点精度抹掉时成立；成否取决于开机时长导致的浮点尾数，属**精度依赖**而非逻辑错误。

修复建议（未执行，避免越界）：把断言改为 `ROUND_BUDGET + GRACE + 0.001`，或对比较加容差 `>= budget - 1e-6`。

### 范围审计
`find ... -newermt "2026-11-18 08:44"` 显示今日仅 12 个代码文件被动过，全部落在各轨所有权内，无越界。

### 本轮暴露的新阻塞（下一轮输入）
1. **FA 供给率 2%**：47 道待跑题只有 1 道能产出 FA，`first_action_for_cve` 对 18 个 CVE 标识 0 命中。P0 机制已完成但无货可注。
2. **`graph_snapshot.py:333-350`** 把 `key_conditions` 收窄为「verified + 有 refs + 关键词启发式」，前端只能收到子集（D 已在前端兜底展示为「已验证但未列入关键条件」）。
3. **`metrics_guard` 无生产调用方**：需在出数脚本入口接入 `assert_feature_live → snapshot → format_comparison`，否则仍是「可被调用」而非「已在拦」。
4. **`backend/app/training/` 整个目录未被 git 跟踪**（含既有 `transitions.py`）：自进化层代码不在版本控制内。
