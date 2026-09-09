# Solver 评分与预测设计

## 1. 目标

对一次 SolveRun 进行量化评分。评分前，模型先分析挑战内容并给出预测值；Run 结束后按实际表现计算指标与总分。

## 2. 指标定义

| 指标 | 定义 | 方向 |
| ---- | ---- | ---- |
| 时间比率 | `actual_seconds / predicted_solve_seconds` | 越小越好 |
| Token 比率 | `actual_tokens / predicted_tokens` | 越小越好 |
| 是否解出 | `status == COMPLETED_SOLVED` | 1 / 0 |

预测值由模型在挑战级缓存，同一挑战的所有 Run 复用同一预测基准。

## 3. 评分公式

权重采用试行版：

```text
解出   = 50
时间   = 30
Token  = 20
效率封顶 = 2.0
```

计算步骤：

```text
time_eff  = predicted_seconds / actual_seconds     # 达标 = 1
token_eff = predicted_tokens / actual_tokens
time_pts  = 30 * min(time_eff, 2.0)
token_pts = 20 * min(token_eff, 2.0)
solved_pts = 50 if solved else 0

total = solved_pts + time_pts + token_pts
```

边界：

- 两项刚好达到预测且解出：100 分。
- 两项都是预测的 2 倍且解出：150 分封顶。
- 未解出：解出项为 0，时间/token 分保留进总分。
- 实际值缺失：对应项不产生分数，`score_status` 标记为不完整。

权重、封顶倍数、公式版本全部写入配置常量，后续调整不影响历史评分（历史评分快照保留）。

## 4. 数据模型

### challenge_predictions

挑战级预测缓存，一个挑战最多一行（最新预测）。

```text
id, challenge_id (unique), prediction_version
status: PENDING / RUNNING / COMPLETED / FAILED
fingerprint                         # 挑战内容指纹
predicted_solve_seconds, predicted_tokens, predicted_tool_calls
difficulty, confidence, rationale_zh, model
usage_json                          # 预测调用自身 token 消耗，不计入 Run
error_code, created_at, updated_at
```

### run_scores

每次 Run 一行。评分时快照预测值，历史不随预测刷新变化。

```text
id, run_id (unique), challenge_id
prediction_snapshot_json            # 评分时预测快照
actual_seconds, actual_tokens, solved
time_ratio, token_ratio
time_points, token_points, solved_points, total_score
formula_version, score_status, error_code, created_at, updated_at
```

## 5. 预测流程

1. `POST /challenges/{id}/runs` 创建 Run 后，异步触发 `request_prediction`。
2. 预测模型按以下优先级选择：
   - 创建 Run 时：使用该 Run 的 `reason_model_config_id`（Coordinator Reason）。
   - 挑战内容变化或手动刷新时：使用该挑战最近一次 Run 的 Coordinator Reason。
   - 没有可继承的 Run 选择时：使用第一个启用、`provider_type=openai_compatible` 且具备 `coordinator_reason` 角色的模型。
3. 显式或继承得到的 Reason 配置必须启用、为 OpenAI 兼容类型、具备 `coordinator_reason` 角色，并配置 `base_url` 与 API Key；否则预测记录为 `PREDICTION_MODEL_UNAVAILABLE`。
4. 若挑战已有 `COMPLETED` 且指纹一致的预测，直接复用。预测缓存不因某次 Run 更换 Reason 模型而自动失效。
5. 否则写入 `RUNNING` 状态，后台任务调用模型；成功后将实际使用的模型名写入 `challenge_predictions.model`。
6. 模型输入仅包含：挑战名、描述、类型、URL、技术栈元数据、附件清单；不包含 Flag、源码、hints。
7. 预测输出 JSON：`predicted_solve_seconds`、`predicted_tokens`、`predicted_tool_calls`、`difficulty`、`confidence`、`rationale_zh`。
8. 预测调用自身 token 只记录在 `usage_json`，不进入 Run 的实际 token。
9. 挑战内容变化（`update_challenge`、附件增删）或手动刷新时重新预测；指纹相同则不重复调用。手动刷新会重新按上述优先级解析模型。

## 6. 评分流程

1. Run 进入终态时，由 `RunFinalizer.reconcile` 调用 `finalize_run_score`。
2. token 聚合：Muteki 优先读 RunEvent 的 `cost.update` 汇总；legacy 回退到 `RunAttempt.input_tokens + output_tokens`。
3. 时间取墙钟 `started_at -> finished_at`。
4. 计算指标与总分，写入 `run_scores`，并触发 `run.score.computed` 事件。
5. 预测缺失或未完成时，评分行以 `NO_PREDICTION` 状态落库，预测完成后可重算。

## 7. API

```text
GET  /challenges/{id}/prediction      # 查询挑战预测缓存
POST /challenges/{id}/predictions     # 手动刷新预测
GET  /runs/{id}/score                 # 查询 Run 评分
```

Run 列表/详情返回 `score` 字段，供前端列表徽标与详情卡展示。

## 8. 前端

- WorkspacePage：新增评分卡，展示总分、三项指标、预测 vs 实际、预测理由。
- RunsPage：新增“评分”列。
- SSE 新增 `run.score.computed` 事件，评分完成后刷新详情页评分卡。

## 9. 边界与后续

- 未解出时时间/token 分保留进总分（用户确认）。
- 试行期使用墙钟时间；后续如需剔除暂停时段再改口径。
- `formula_version` 保留字段，权重调整后可通过版本号区分历史分数。
