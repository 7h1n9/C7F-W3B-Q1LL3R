# RACE 窗口要不要放宽 —— 2026-11-05 实测分析

背景：用户观察到「agent 速度似乎跟不上」，怀疑 RACE 窗口太小。

结论：**窗口不是瓶颈。1500 秒已接近最优，放宽的边际收益只有 +3.7%。**

---

## 一、先纠正一个误解：窗口实际是多少

| 参数 | 值 | 位置 |
|---|---|---|
| `ARENA_RUN_BUDGET_SECONDS` | 1800s | 整场 Run 上限 |
| `ARENA_WORKER_ROUND_SECONDS` | **1500s** | 单轮 Worker 上限 |
| `RUN_CONVERGENCE_SECONDS` | 120s | 收敛窗口 |
| `RUN_TIMEOUT_SECONDS` | 2100s | 外层等待（预算+300） |

生产路径 `muteki_runtime.py:707` 实际计算：

```python
worker_timeout_seconds = max(1, min(self._worker_round_cap(), run.max_runtime_seconds))
                       = min(1500, 1800) = 1500
```

实测验证：Solr XXE 那轮 `worker-1` 跑 **1535 秒**（t+82 → t+1617），贴着 1500 上限，符合设计。

> 注意 `MutekiConfig.worker_timeout_seconds = 480` 那个默认值**与生产无关**——
> 生产走 `UpstreamRuntimeGraph`（`muteki_runtime.py:559` 传 `default="upstream"`），
> 480 是 `MutekiGraph`/兼容路径的默认，容易被误读为「单轮只有 8 分钟」。

---

## 二、解出率随时长的分布（480 run，含 169 solved）

| 时长 | 总数 | 解出 | 解出率 |
|---|---|---|---|
| <2min | 46 | 24 | 52% |
| 2-5min | 110 | 53 | 48% |
| 5-10min | 87 | 46 | 53% |
| 10-15min | 121 | 25 | **21%** |
| 15-20min | 34 | 12 | 35% |
| 20-25min | 13 | 3 | 23% |
| **25-30min** | 65 | 5 | **8%** |
| >30min | 4 | 1 | 25% |

**10 分钟内解出率 48-53%，之后断崖式下跌，25-30 分钟只剩 8%。**

也就是说：熬满 1500 秒单轮的那些 Run，绝大多数本来就没救。

## 三、窗口上限的模拟（同一批数据）

| cap | 保住解出 | 损失解出 | 总墙钟 |
|---|---|---|---|
| 600s | 123 | 46 | 3505 min |
| 900s | 148 | 21 | 4500 min |
| 1200s | 160 | 9 | 4983 min |
| **1500s（当前）** | **163** | **6** | **5351 min** |

放宽到 1800s 最多再救 6 个（+3.7%），代价是每场多耗时间。**不值得。**

## 四、按轮次看解出率（反直觉）

| 轮次 | 总数 | 解出 | 解出率 |
|---|---|---|---|
| 0 | 76 | 1 | 1% |
| **1** | 126 | 71 | **56%** |
| 2 | 207 | 73 | 35% |
| 3 | 24 | 10 | 42% |
| 9 | 7 | 3 | 43% |
| ≥11 | 16 | 1 | 6% |

**1 轮解出率最高（56%）**。多轮意味着第一轮没打对，后面基本翻不回来。
这进一步说明：问题不在「给的时间不够多轮」，而在「第一轮有没有找准路子」。

## 五、真正的浪费：75 个 Run 压根没开打

未解 312 个 Run 里：

| 类型 | 数量 | 说明 |
|---|---|---|
| `NO_HEALTHY_WORKER_PROFILE` | **75** | 引擎全挂，**0 个 worker step**，248 秒内关闭 |
| 打了但没解出 | 236 | 正常失败 |
| 其他 | 1 | — |

这 75 个占了未解总数的 **24%**，而且**一个命令都没执行**。它们不是「agent 太慢」，
是「agent 根本没上场」。

### 为什么保护机制没生效（已修）

`arena_queue.py:686` 有这段保护：

```python
if _is_infrastructure_no_attempt(outcome.run_status_reason):
    item.attempt_count = max(0, item.attempt_count - 1)
    await _finish(session, item, PENDING, "No usable Worker engine ...")
    return
```

设计意图：引擎不可用不算这道题的失败，退回队列等渠道恢复。

**但它从来没触发过。** 实测 75 个 Run 里：

- **74 个 `last_error_code IS NULL`**
- 1 个是 `MUTEKI_NATIVE_FINALIZED_RECONCILED_UNSOLVED`

原因：Supervisor **不会把 Coordinator 的 stop_reason 写到 Run 行上**。
`NO_HEALTHY_WORKER_PROFILE` 只存在于 `muteki.dead_end` 事件的 payload 里。
所以 `outcome.run_status_reason` 永远是 `""`，`_is_infrastructure_no_attempt("")` 恒 False。

修复：`arena_solve._dead_end_reason()` 从第一条 `muteki.dead_end` 事件读 reason。
必须取**第一条**——Coordinator 在 finalize 时会再发一条
（`ROUND_BUDGET_EXHAUSTED_BEFORE_REASON` 或裸 `WORKER_TIMEOUT`），那条描述的是收尾不是起因。

实测验证：

```
408bd532  reason='NO_HEALTHY_WORKER_PROFILE'  -> infra_no_attempt=True    （退回队列）
1e53d522  reason='RACE_WORKER_TIMEOUT'        -> infra_no_attempt=False   （正常计数）
04186c09  reason='RACE_WORKER_TIMEOUT'        -> infra_no_attempt=False   （正常计数）
```

测试 `backend/tests/test_arena_dead_end_reason.py`（8 条）。

---

## 六、处置决定

1. **RACE 窗口不改**。1500 已近最优，数据不支持放宽。
2. **`[AppWeb]CVE-2018-8715` 从队列移除**。3 次尝试全部在 48 秒转 `error`
   （对照组 `[Node]CVE-2017-14849` 同 practice ground 下 10 秒就 ready），
   本地无配方无 PoC，连靶机都摸不到，出不了配方。备份在 `.probe/backup/queue-8715-before.json`。
3. **修复「无引擎可用」被误判为失败**（见第五节）。

## 七、后续真正值得做的

- 那 75 个 Run 退回队列后，会在渠道恢复时自动重试 —— 这是白捡的。
- 25-30 分钟那 65 个 Run 解出率只有 8%：它们共同特征是「打了很久但方向错」。
  要提升的是**第一轮的判断质量**（技法宝库、exploit-first 配方），不是给更多时间。
