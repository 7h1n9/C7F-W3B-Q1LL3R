# 2026-10-19 benchmark 断点收口与测试基线归零

本轮把 `docs/archive/2026-09-10/solver-capability-todo.md` §8 里挂着的三条「既有缺口」清掉，并把长期
16 条失败的测试基线归零。改动全部落在执行侧与测试替身，**未触碰解题链路**。

---

## 1. 三条缺口与结论

| # | 缺口 | 结论 |
|---|---|---|
| 1 | 停掉的 benchmark CLI 会把 Run 留在 `RUNNING` | 已修：信号 → 停止标志 → 官方取消握手 → 落库 |
| 2 | benchmark 缺「超时即杀」硬预算 | 已修：executor 侧 wall-clock guard |
| 3 | 终态 Run 的运行中容器会残留 | 启动期已有 reaper；**运行期**的残留由第 1 条覆盖，`kill -9` 场景仍靠下次启动 reconcile |

第 3 条不再单独加周期性全库扫描：`_runs_owning_a_container()` 每次要一条
`docker ps -a`，而它解决的问题（进程非正常退出）恰恰会在下一次启动时被
`reap_terminal_muteki_containers()` 处理。加周期性扫描是拿常态成本换一个本就
有兜底的边缘场景。

## 2. 中断路径为什么必须改在 executor 里

`asyncio.run()` 收到 `SIGINT` 时会先 `cancel_all_tasks()` 再关闭 loop。
`run_supervisor.run_background()` 的 `finally` 只做「注销任务 + 释放锁」，
真正的收尾（`cancel_muteki_with_handoff()` → Coordinator Finalize → 容器 teardown）
一个都不会跑。于是：

- Run 行停在 `RUNNING`，下次 `GET /runs/{id}` 报一个幽灵任务；
- 容器继续跑，直到人工 `docker rm -f` 或下次启动 reconcile。

修法分两层，缺一不可。

### 2.1 CLI：把信号变成标志，而不是异常

`app/benchmark/__main__.py::_install_stop_request()`

```python
stop = threading.Event()

def _handle(signum, _frame):
    if stop.is_set():
        raise KeyboardInterrupt      # 第二次：恢复默认行为，保证能杀掉
    stop.set()
    print(f"[matrix] {name}: cancelling the in-flight Run; send again to abort immediately")

for attribute in ("SIGINT", "SIGTERM"):
    signal.signal(getattr(signal, attribute), _handle)
```

要点：

- 用 `threading.Event` 而非 `asyncio.Event`：信号处理器可能不在 loop 线程上跑，
  只有前者能从处理器里安全写入。
- 处理器只置位、不抛异常，所以 loop 不会被拆掉，后续的 `await` 仍然可用。
- 第二次信号重新抛出：一个卡住的进程仍然必须能被 Ctrl+C 杀掉。

### 2.2 Executor：轮询 Run，而不是无脑 await

`app/benchmark/experiment.py::RealRunExecutor._drive_run()`

```python
task = asyncio.create_task(supervisor.run_background(run_id))
deadline = time.monotonic() + self.hard_runtime_seconds
while True:
    done, _ = await asyncio.wait({task}, timeout=RUN_GUARD_POLL_SECONDS)
    if done:
        return await task
    if self._stop_requested():
        return await self._abandon(supervisor, run_id, task, reason="BENCHMARK_INTERRUPTED")
    if time.monotonic() >= deadline:
        return await self._abandon(supervisor, run_id, task, reason="RUN_HARD_TIMEOUT")
```

`_abandon()` 做三件事，顺序不能换：

1. `supervisor.cancel_muteki_with_handoff(run_id)` —— 官方取消 + Finalize 握手 + 容器 teardown；
2. `task.cancel()` 并 `gather`，确保 supervisor 任务真的结束；
3. `run_lifecycle.cancel_run(session, run_id, ...)` —— 写终态行。

第 3 步是 CLI 必须自己做的：`cancel_muteki_with_handoff()` 有意识地**不写** Run 行，
那是 API 端点 `POST /runs/{id}/cancel` 的职责。CLI 没有端点，少这一步就还是留
`RUNNING`。

一个边界被显式处理：如果 Run 在守护触发的同一瞬间自己收尾了（Coordinator 先拿到
结果），第 3 步之前会读一次状态，`parse_run_status(current) in TERMINAL` 就直接
返回真实状态、不改写成 `CANCELLED`——否则会丢掉一个已经解出的 flag。

## 3. 硬预算

`--hard-runtime-seconds`（默认 `--runtime-seconds + 180s`，`--runtime-seconds`
未给时用 `DEFAULT_MAX_RUNTIME_SECONDS + 180`）。

比 Run 自身的 deadline **宽** 180 秒是刻意的：正常路径应当由 Coordinator 的
`RUN_CONVERGENCE_SECONDS` 先收尾，硬预算只兜「自己不会停」的 Run。把它设成
和 `--runtime-seconds` 相等，会让每次到点都走强制取消，凭空制造
`MUTEKI_FINALIZE_GRACE_EXCEEDED` 事件。

被强制取消的 Run 返回 `ExecutionResult(status="CANCELLED", error=reason)`，
`evaluation_report=None`——摘要据此区分「中止」与「没解出来」，不会把中止计入
分母的分子之外还污染分母。

## 4. 摘要不再假装完整

`ExperimentRunner.run()` 在每个 task **之前**检查停止标志（不是之后：被打断的
task 已经写了自己的 artifact，未开始的 task 只是没跑）。

```json
{"stopped_early": true, "completed_tasks": 3, "planned_tasks": 20}
```

CLI 以 `130` 退出（`EXIT_INTERRUPTED`），与 shell 对「被信号终止的子进程」的
约定一致，包装脚本可据此区分「操作者停的」与「任务失败了」。

## 5. 测试基线：16 failed → 0

### 5.1 三条是替身漂移，不是产品缺陷

| 症状 | 真因 | 修法 |
|---|---|---|
| `assert 2 == 1`（reap） | `reap_terminal_muteki_containers()` 现在会先问 Docker「这条 Run 还有容器吗」，测试里的假 `container_exec` 没有 `_docker`，`_runs_owning_a_container()` 抛错后按「名单不可用」回退成全部回收 | 假模块补 `_docker` / `_run_container_name` / `_safe`，并把断言改成「终态 + 重启后的 RUNNING 都回收、legacy 不动」 |
| `_Session.get() got an unexpected keyword 'populate_existing'` | 替身签名没跟 `session.get(..., populate_existing=True)` | 加 `**_kwargs` |
| `assert 0 == 1`（reap orphan） | 同上：`run-orphan` 不在真实 `docker ps` 名单里 | patch `_docker` 返回该容器名 |

### 5.2 十二条是同一个注册键漂移

`WorkerManager.execute()` 用 `action.metadata.get("backend", "runner")` 选后端，
而 `DeterministicPlanner` **不写** `backend`——所以默认落到 `"runner"`。
测试却把 mock 注册在 `"mock"` 键下，于是每次执行都路由到默认的
`RunnerWorker`（真实 HTTP），失败后被判 `ACTION_FAILED`，阶段也就不推进
（`BASELINE` 停在原地，`VALIDATION` 不跳 `EXPLOITATION`）。

一处根因，影响：`test_solver_loop`(4)、`test_solver_action_recovery`(4)、
`test_solver_audit_events`(2)、`test_strategy_switching`(1)、
`test_worker_adapter`(1)、`test_phase23a_context_security`(1)。

修法：把「通过 loop 驱动」的注册点改成 `workers={"runner": ...}`。直接构造
`ActionIntent(metadata={"backend": "mock"})` 的用例保持原样——那条路径测的正是
显式后端路由。

> 这不是把测试改绿：`backend` 默认为 `runner` 是 `WorkerManager` 的既有契约
> （生产路径必须如此）。测试写 `"mock"` 是把一个只在测试里存在的名字当成了默认值。

### 5.3 另外两条与真实数据库耦合

| 症状 | 真因 | 修法 |
|---|---|---|
| `test_knowledge_api` 两条 `assert 8 == 1` | `GET /knowledge/experiments` 会合并 `db_pair` 实验记录；开发库里跑过 control experiment，就成了额外条目 | 加 `_no_db_pairs` 替身，只测目录读取 |
| `test_model_config_delete` `arena_submissions still RESTRICTs` | `0044` 只覆盖它创建时**已存在**的表；`arena_submissions`(0046)、`arena_connections.default_model_config_id`(0047) 都是之后才建的，带着 MySQL 默认 `RESTRICT` | 新增迁移 `0051`，两列都改 `SET NULL` |

### 5.4 `test_muteki_run_heartbeat` 单独跑必失败

两个独立原因叠在一起：

1. `sqlite:///:memory:` + `StaticPool`，测试在事务中途 `cancel()` 心跳 → 连接被
   invalidate → 池里只剩一个空库，下一次 checkout 重建，`no such table: solve_runs`。
   → 改用 `tmp_path` 文件库。
2. `interval_seconds=0.01` 被内部 clamp 到 `0.1`，测试却固定 `sleep(0.15)` 再取消。
   一个 tick 是「sleep + 一次 session + 三次读 + commit」，冷库上跑不完。
   → 改成「观察效果」：轮询直到 `heartbeat_at > now`，最多 5 秒，再取消。

第 2 点比第 1 点更值得记：**测试等的是效果，不是时长**。

## 6. 验证

| 检查 | 结果 |
|---|---|
| `pytest tests/` 全量 | **1721 passed**，0 failed（改前 16 failed / 1702 passed） |
| `pytest tests/test_benchmark_abort_guard.py` | 6 passed（新增） |
| `pytest` 六个既有 benchmark 套件 | 79 passed |
| `python -m app.benchmark --dry-run` | `4 task(s) planned` |
| `alembic upgrade head` | `0050 → 0051_arena_submission_model_config` |
| `ruff check`（本轮改动 + 新增的全部文件） | All checks passed |

`test_benchmark_abort_guard.py` 覆盖：

1. 停止标志 → `cancel_muteki_with_handoff` 被调用 + Run 落 `CANCELLED` + `finished_at` 非空；
2. 硬预算到点 → `RUN_HARD_TIMEOUT` + 同样落库；
3. Run 正常结束 → **不**调用取消，返回原 `RunOutcome`（仍走评估）；
4. Run 在守护触发瞬间自己收尾 → 保住 `COMPLETED_SOLVED`，不改写；
5. 矩阵停止 → 不再启动下一个 task，summary 带 `stopped_early`；
6. 信号 → 只置位；第二次才抛 `KeyboardInterrupt`。

## 7. 仍未做

| # | 事项 | 理由 |
|---|---|---|
| 1 | 运行期周期性容器回收 | 见 §1 第 3 条：收益低于常态成本，且启动 reconcile 已兜底 |
| 2 | `kill -9` 后的 Run 状态修复 | 只能靠下次启动 `_reconcile_persisted_runs()`；进程内无法自救 |
| 3 | 真实 ≥10 对配对矩阵 | 仍需算力 + 靶场 + 3307 应用库，未在轮内执行 |
