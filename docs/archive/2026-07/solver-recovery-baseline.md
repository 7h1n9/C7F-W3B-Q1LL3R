# 解题引擎恢复基线（2026-07-14）

此文档记录恢复冻结开始前从本机 MySQL（3307）读取的事实；统计不修改原始 Run、证据或工作区。

## 汇总

| 项目 | 基线值 |
| --- | ---: |
| 总 Run 数 | 37 |
| Codex SDK 成功率 | 8 / 19（42.1%） |
| OpenAI Compatible 成功率 | 5 / 18（27.8%） |
| `FAILED_ENGINE` | 17 |
| `COMPLETED_UNSOLVED` | 5 |
| 非终态 Run | 0 |
| `RUNNING` Attempt | 3 |
| 过期 Lease | 3 |
| 终态 Run 残留 Lease | 3 |
| Mock Codex Run | 7 |
| Codex ToolCall | 393 |
| OpenAI ToolCall | 197 |
| 文件读取失败 | 0 |
| Skill 激活失败 | 78 |
| Action 拒绝 | 35 |
| Bridge 超时/不可达事件 | 8 |
| Workspace 同步失败线索 | 15 |

恢复开始时已使用受约束的启动协调清理 3 个过期 Lease，并将对应 3 个 `RUNNING` Attempt 标为 `ABORTED`。不会删除其他实例仍有效的 Lease。

## 最近 10 个 Run

1. `3f2be0b7-596c-4505-8ccb-6e0e5e3117e4` — Codex SDK，`FAILED_ENGINE`
2. `663af43c-8127-42cc-a4b3-d2d2bdea23de` — Codex SDK，`FAILED_ENGINE`
3. `663400c0-8e2c-46b8-a12d-f5ae635a863c` — Codex SDK，`FAILED_ENGINE`
4. `d56d333b-4052-406f-9012-f3bc33b9992a` — Codex SDK，`FAILED_ENGINE`
5. `3db4142c-2d7a-4f28-b614-62039cebd33b` — OpenAI Compatible，`COMPLETED_UNSOLVED`
6. `a89e0402-ddd0-454a-be91-4e2fb4b10d11` — Codex SDK，`CANCELLED`
7. `deda4421-23ae-49e2-a197-c91b7217a476` — OpenAI Compatible，`COMPLETED_UNSOLVED`
8. `cf5a4866-5e5b-45b2-b68d-6341c53b7349` — Codex SDK，`FAILED_ENGINE`
9. `0ae5494e-4fb1-4855-9d92-eb27ec024cf9` — Codex SDK，`FAILED_ENGINE`
10. `36ee29fc-dde2-492b-8122-05476cd96cd8` — OpenAI Compatible，`COMPLETED_UNSOLVED`

## 重点轨迹结论（已脱敏）

- `d56d333b…`：Bridge `/resume` 读超时，之后被错误地转为 `FAILED_ENGINE`。
- `663400c0…`：模型尝试未注册的 MCP 工具，策略拒绝被错误地附带 `FAILED_ENGINE`，还生成了不应有的最终报告。
- `3f2be0b7…`：模型明确报告没有 `ctfctl`/Tool Gateway；其后又发生 Bridge ReadTimeout。
- `3db4142c…`：空身份 Skill 激活反复被拒绝，8 次无进展后错误结束为未解出。
- `36ee29fc…`：重复 `file_read`/动作拒绝导致无进展计数递增，最后过早生成未解出报告。
- `deda4421…`：已完成 HTTP 取证但未进入更合适的后续假设维度，最终按旧策略结束。

历史 Codex ToolCall 均为原生 `command_execution`、`node_repl.js` 或 `web_search`，没有可作为真实 Gateway 成功证据的 ctfctl 调用。因此该基线不能作为新的 Codex 成功率依据。
