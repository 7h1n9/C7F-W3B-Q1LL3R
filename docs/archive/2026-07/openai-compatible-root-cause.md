# OpenAI Compatible 解题链路根因（2026-07-13）

## 真实数据库基线

本报告基于当前连接的 MySQL `127.0.0.1:3307/ctf_agent`，而不是静态代码或旧备份：

| 指标 | 结果 |
| --- | ---: |
| Run 总数 | 15 |
| Codex SDK | 9（8 已解出、1 已取消） |
| OpenAI Compatible | 6（2 未解出、4 引擎失败） |
| ToolCall 总数 | 380 |
| AgentTurn（OpenAI） | 28 |
| Skill 推荐事件（OpenAI） | 51 |
| AgentAction 解析失败记录 | 9 |

OpenAI Compatible 当前成功解出数为 0，不能输出 READY。数据库中没有证据表明这是题目本身全部不可解；失败集中在 Provider 协议/配额错误和模型输出格式，而不是 Runner 连通性。

## 六个重点 OpenAI Run

- `ddba86fe-9117-4e81-a93a-a903438bdb08`：完成但未解出。
- `10db33a6-ddf9-4c06-b2999451f08b`：完成但未解出。
- `92eb37c0-9cde-4e89-8893-9528528a24ee`：HTTP 402 配额耗尽，原实现错误包装成 `AGENT_ACTION_PARSE_FAILED`。
- `ac19b19a-6eba-4e8c-9cc4-f4684b2af766`：同上，并叠加历史 `matched_triggers` 字段兼容问题和时区问题。
- `15f8d4f0-c698-49be-aa3c-6c11be0721ec`：HTTP 402 配额耗尽。
- `b138d852-b72a-41e4-a960-c23eb48bc5d7`：模型返回 `type=ToolAction`，严格 discriminator 只接受 `tool`，因此失败。

## 已修复的根因

1. Provider HTTP 400/401/402/403/429/5xx 现在分别记录 `MODEL_BAD_REQUEST`、`MODEL_AUTH_FAILED`、`MODEL_QUOTA_EXCEEDED`、`MODEL_PERMISSION_DENIED`、`MODEL_RATE_LIMITED`、`MODEL_UNAVAILABLE`；402 不再伪装成 Action 解析失败。
2. 对明确的 `ToolAction`、`SkillAction`、`FinishAction` 别名做有限规范化，未知 discriminator 仍由 Pydantic 严格拒绝。
3. 保留限流退避、`Retry-After` 和 Provider 冷却窗口；重试只用于可重试网络/服务状态。
4. 上下文去重、裁剪、最新 ToolModelView 后置、Flag 动态化和 Context Budget 统计已启用。

## 当前结论

由于真实 Provider 仍返回 HTTP 402，OpenAI Compatible 尚未完成成功题目回归，因此当前结论为：

```text
OPENAI_COMPATIBLE_SOLVER=NOT_READY
```

恢复 Provider 配额或切换到可用的 OpenAI-compatible endpoint 后，应至少重新执行弱口令后台、Easy JWT、备份泄露、简单 SQL 注入和路径穿越各三次，并检查中文 WP、Session 复现和 Skill Candidate 污染扫描。
