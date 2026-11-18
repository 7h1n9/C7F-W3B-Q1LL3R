# 2026-07-26 方法论与 Runner 回归基线

本轮基线来自任务提供的数据库快照与重点 Run 记录；仓库未包含
`2026-07-26-23-16.sql`，因此以下数字作为外部观测基线保存，不在本地伪造重算。

## 最新 Run

| 指标 | 值 |
|---|---:|
| Run | `479cc015-5f99-46f9-9afd-236ac9a15bc0` |
| Agent Step | 25 |
| Run Effective Tool Count | 58 |
| LogicalToolCall Rows | 140 |
| RunEvent | 738 |
| Observation | 108 |
| Artifact | 132 |
| Attempts | 5 |
| Tool Failed Event | 69 |
| Compaction Generation | 0 |
| Script Records | 0 |

另一重点 Run：`920060f6-3ba3-4b66-a832-a1e4d3efe00f`。

## 自动化交付基线

```text
sqlmap_run = 0
script_run = 0
python_run = 0
sandbox_exec = 0
```

## 根因假设

1. MCP 将后端非 2xx 响应压扁成 `CTFCTL_ERROR: Internal Server Error`，导致
   Runner 已完成与 Backend 持久化失败无法区分。
2. 外层 `ctfctl.*` 与内层工具调用缺少显式的逻辑身份与预算参与标记，导致
   预算、压缩和 UI 可能重复统计。
3. ToolGateway 在返回工具结果前同步执行压缩，压缩失败会污染工具交付路径。
4. Runner 当前按任务立即创建 asyncio task，缺少全局/Run/工具类别并发限制，且
   固定等待窗口不能覆盖 SQLMap 和长脚本。

本文件只记录基线与待验证假设，不将答案引导型脚本计入自主解题成功率。
