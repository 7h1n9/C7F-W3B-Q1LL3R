# CAP-03 配对 A/B 矩阵 —— 可行性评估与执行方案

> 日期：2026-10-15
> 结论一句话：**测量基础设施已完备且回归覆盖到位；一次 ≥10 对的真实矩阵目前跑不完，
> 阻塞点有三，其中两个是硬阻塞。下面给出可执行方案。**

---

## 1. 基础设施清单（已存在，无需新建）

| 模块 | 职责 | 状态 |
| --- | --- | --- |
| `backend/app/benchmark/__main__.py` | CLI 入口：`python -m app.benchmark`，负责决定输入与退出码 | 完整 |
| `backend/app/benchmark/experiment.py` | `ExperimentRunner`（manifest / fingerprint / plan / resume / summary）+ `RealRunExecutor`（真实 SolveRun 经 `run_supervisor`） | 完整 |
| `backend/app/benchmark/range.py` | 靶场供给：每次重复换端口 + 换 flag（CAP-34 运行时增强） | 完整 |
| `backend/app/benchmark/evaluation.py` | 逐 Run 评分 → artifact | 完整 |
| 统计层 | 配对差值 + 精确 McNemar（二分类）+ 百分位 bootstrap CI（2000 次，seed 20260101） | 完整 |
| 回归 | `test_benchmark*.py` 共 **79 项全绿** | ✅ |

关键常量：`ARMS = ("off", "on")`、`MIN_PAIRED_SAMPLES = 10`。
臂 → Run 的绑定在 `RealRunExecutor._create_run`：`hints["knowledge_mode"] = request.arm`。

本次新增（补上一处真实缺口）：CLI 增加 `--runtime-seconds` / `--total-runtime-seconds`，
`RealRunExecutor` 改为 `None` 即回落到自身默认值（`900` / `3600`）。此前每个 Run 的墙钟
预算无法从命令行表达，只能改代码 —— 见 §3 硬阻塞 B。

---

## 2. 本次实测证据（不是推断）

| 观测 | 结果 |
| --- | --- |
| `--repeats 3 --dry-run` | 12 个 task 入计划 = **2 个可跑用例** × 2 臂 × 3 次重复 |
| 被跳过用例 | `ssrf-internal`、`xss-reflected` → `SKIPPED_REQUIRES_RANGE_MISSING`（`requires_range: web-vuln-lab`） |
| 仓库靶场 | `ranges/` 下**只有** `asset-warranty-mysql` |
| 靶场拉起 | `docker compose up -d` → `asset-warranty-web` healthy，`GET /health` = **200** |
| 真实 Run | 冒烟跑出 `dd2cc8f8-…`，`hints_json.knowledge_mode = "off"` —— 臂正确落盘 |
| 每 Run 墙钟 | 实测 **1000 s 仍未终态**（默认上限 900 s），23 轮 Worker、70 个 intent、13 个 dead_end |
| 求解结果 | **0 个 flag 事件**（无 `flag_candidate` / `flag_found`）→ 该配置下未解 |
| 长跑复核 Run | `47d7c1d7-…`（`off` 臂，题 `benchmark:sql-injection-golden`）跑到 **1367 s** 仍未终态：44 agent step / 15 tool call / **21 个 `dead_end` / 0 个 flag 事件** |
| 墙钟硬边界 | `--runtime-seconds`（默认 900）是**软**上限：只触发收敛、不杀 Worker；实际边界是 `--total-runtime-seconds`（默认 3600） |
| P1-1 观测复核 | 同一 Run 落库 `muteki.worker_step` **151 条**，随运行单调递增（26 → 87 → 145 → 151）→ 修复在生产路径上**持续**生效 |

顺带坐实并修掉了一个 P1-1 的遗留缺口：修复前该链路全程 `worker_step` 行为 **0**。根因两条 ——
（1）`OfficialWorkerAdapter._execute_sync` 的生产分支是 `chat_completions`（`openai_compatible` 引擎走
`OpenAICompatibleWorker`，模型调用在宿主侧），**完全不经过** `CliSolver` / `driver.parse_stream_steps`，
而第一个 driver 级 observer 只覆盖了 `cli_solver` 那一支；（2）`emit_event` 把落库任务调度在
**调用线程当前 loop** 上，而 Worker 上报 step 时跑在自己的短命 `asyncio.run` loop 里，loop 一关
任务即被取消、事件静默丢失 —— 同一条 `emit_event`，`phase_changed` 能落库而 `worker_step` 不能，
就是这个原因。两条均已修（`OpenAICompatibleWorker.step_callback` + `_publish_worker_step` 跨 loop 搬运），
修复后实测同一链路产出 **151 条**。

---

## 3. 阻塞点

### 硬阻塞 A：可达配对数被靶场几何锁死

`asset-warranty-mysql` 只暴露**一个**可注入参数：
`POST /api/warranty/check` 的 `department`（字符串拼接）。两个可跑用例
（`sql-injection-golden`、`sql-injection-boolean`）打的是**同一个端点、同一个参数**。

于是凑够 `MIN_PAIRED_SAMPLES = 10` 的唯一途径是**把重复次数堆到 5**
（2 用例 × 5 次 = 10 对）。这意味着"达标"的统计量只覆盖 1 个漏洞 / 1 个端点；
`by_case` 只有两个几乎同质的键。

→ 要真正提高多样性，必须补 `web-vuln-lab` 靶场（或给现有靶场加新的可注入面 / 新端点）。
这是内容工作，不是 CAP-03 的测量工作。

### 硬阻塞 B：墙钟成本 ≈ 5–6 小时连续模型调用

实测单 Run 会打满 900 s 预算**并且会超**（软上限：超时后进入收敛而非硬杀）。

- 10 对 = 20 个 Run × ~15 min ≈ **5 小时**
- 期间要跑 20 个真实 muteki 容器、20 轮完整解算

本次加的 `--runtime-seconds` 让这个成本第一次可表达（例如 `--runtime-seconds 600`
≈ 3.3 小时），但它仍然是**需要无人值守数小时**的任务，不适合放在单次会话里跑完。

> ⚠️ **`--runtime-seconds` 不能设得太小**（实测踩过）。实测 `--runtime-seconds 120
> --total-runtime-seconds 180` → Run 直接 `FAILED_ENGINE` / `MUTEKI_RUNTIME_ERROR`，
> **一个 Worker 轮都没启动**。原因是 §3.3 的派发预留规则「跑不完的一轮不准启动」：
> 一轮的预算是 `worker_timeout`（现配 720s），窗口装不下一轮时 `_spawn()` 直接拒发。
> 所以下限大约是 `worker_timeout + RUN_CONVERGENCE_SECONDS`，**别拿它当"2 分钟快速冒烟"用**。

### 软阻塞 C：A/B 信号可能退化成零信息

实测 `off` 臂 17 分钟 / 23 轮 Worker / 0 个 flag。**复核已确认这不是个例**：`47d7c1d7` 这次
`off` 臂跑到 1367 s（22m47s）/ 44 agent step / 15 tool call / **21 个 `dead_end`**，
同样 **0 个 flag 事件** —— 即 `off` 臂在现有靶场用例上是**跑满预算也不解**。

若两个臂都解不出，10 对里全是**同向失败对**：`on_only = off_only = 0`，`success_delta = 0`，
McNemar 无失谐对。

此时 `sample_adequate` 会是 `true`（对数凑够了），但结论零信息 —— 这比
`descriptive_only` 更危险，因为门禁会**放行**一个没有内容的结论。

→ 跑矩阵之前，应先确认**至少一个臂在预算内能解**。可以先用单臂小样本确认：
`--arms on --repeats 1`。解不出就先调模型/引擎，而不是堆重复次数。

---

## 4. 可执行方案

前提：靶场已拉起、MySQL 容器在跑、至少一个可用的 worker 模型，且**后端已停**。

> ⚠️ **必须停掉后端**：后端与 benchmark CLI 都占 `MUTEKI_CONTROL_PORT=9100`，
> 并存时容器侧 `runtime_agent --connect host.docker.internal:9100` 会连到错的进程，
> 探针会在 45~110 s 内直接 `FAILED_ENGINE` / `MUTEKI_RUNTIME_ERROR`（**一个 Worker 轮都没启动**），
> 看起来像"靶场坏了"，其实是端口争用。
>
> ⚠️ 若中途 kill 掉 CLI，Run 会**留在 `RUNNING`**（没有进程驱动它）。收尾不要手改 SQL，
> 启动后端后 `POST /api/v1/runs/{id}/cancel` 即可（走 Finalize 交接 → 租约回收 → 终态投影）。

```powershell
# 1) 靶场
cd D:\desktop\毕业设计\C7F-W3B-Q1LL3R\ranges\asset-warranty-mysql
docker compose up -d
# 探活： curl.exe http://127.0.0.1:8383/health   -> 200

# 2) 先看计划，不烧 Run
cd D:\desktop\毕业设计\C7F-W3B-Q1LL3R\backend
..\.venv3.11\Scripts\python.exe -m app.benchmark --repeats 5 --dry-run

# 3) 先确认信号存在：单臂单次
..\.venv3.11\Scripts\python.exe -m app.benchmark --arms on --repeats 1 `
  --runtime-seconds 600 --output-dir data\experiments\cap03-probe

# 4) 正式矩阵（10 对）—— 无人值守，中断后加 --resume 续跑
..\.venv3.11\Scripts\python.exe -m app.benchmark `
  --repeats 5 --arms off,on --randomize-range repeat `
  --runtime-seconds 600 --total-runtime-seconds 900 `
  --output-dir data\experiments\cap03-matrix
```

说明：

- `--randomize-range repeat` 每次重复换端口 + 换 flag，避免 Worker 记住上一轮的地址。
- 结果读 `<output-dir>\summary.json` → `paired_effect`（`success_delta`、
  `success_delta_bootstrap_ci_95`、`mcnemar.p_value`、`sample_adequate`、`claim`）。
- 每个 task 的产物独立落 `<output-dir>\artifacts\<case>__<arm>__r<N>.json`，
  中断只丢当前 task；`--resume` 复用已完成产物。
- 退出码：`0` 完成 / `2` 配置错 / `3` 有 task 失败 / `4` preflight 未就绪（一个 Run 都没烧）。

---

## 5. 验收对照

| CAP-03 验收条件 | 状态 |
| --- | --- |
| 同一 manifest 重跑得到结构一致的输出目录 | ✅ `test_resume_reuses_completed_artifacts`、`test_start_refuses_a_non_empty_output_without_resume` |
| 汇总结果可由原始日志重算 | ✅ `load_raw_artifacts` + `build_summary`，`test_benchmark_paired_statistics.py` 9 项 |
| `case × arm × repetition` 批量 runner（含靶场重置、暂停恢复、manifest 一致性跳过） | ✅ CLI + `ExperimentRunner` + `range.bring_up` |
| 配对差值 + McNemar / bootstrap CI | ✅ `_paired_effect` |
| **真实 ≥10 对矩阵已执行** | ⛔ 未完成 —— 见 §3（A/B 为硬阻塞，C 为前置条件） |

## 6. 解阻塞的最小任务清单

1. **（内容）** 补 `web-vuln-lab` 靶场，或给 `asset-warranty-mysql` 增加第二种漏洞面，
   让 4 个用例都能跑 —— 直接解决 §3-A 的多样性问题。
2. **（模型/引擎）** 让 `on` 臂在 `--runtime-seconds 600` 内至少解出一次 —— 解决 §3-C。
   这是矩阵有意义的前提。注意 `off` 臂已实测跑满预算（1367 s）也不解，
   所以矩阵是否有信息量**完全取决于 `on` 臂**；`on` 臂也解不出的话，堆到 10 对只是把一个
   零信息结论标成 `sample_adequate = true`。
3. **（可选）** 需要一个"硬预算"开关（超时即杀），把 §3-B 的墙钟从"约 5 小时"变成上界明确。
   现在 `max_runtime_seconds` 是**软**上限（触发收敛协商，不杀 Worker）。
