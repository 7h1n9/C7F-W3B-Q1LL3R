# 断点：L1 第一轮判断质量（2026-11-05 17:26 暂停）

关机前状态。回家后从「### 下一步」第一条继续。

---

## 已定方向

用户选择：**L1 假设强制**（先做），验证方式 **10 题 A/B 比步数**。

放弃的选项（供回看，别重复讨论）：
- L2 难度估计/分支预算 —— 排在 L1 之后
- L3 训练/RLVR —— **必须等 `fresh_reproduction_verified` 不为 0**，现在全库仍为 0
- 先修靶机故障识别 —— 记在遗留项

## 依据（我自己的实测，非推断）

63 条未解 vs 30 条已解 Run：

| 指标 | 未解 | 已解 |
|---|---|---|
| 每 Run 命令数（中位） | 75 | 54 |
| 命令去重率 | 99% | 100% |
| 单条长命令占比 | 2% | 3% |
| 时长中位 | 25min | 9min |

⇒ 不是空转、不是卡在慢工具，是**持续在错方向上做功**。

典型案例 `5cb19393` `[Confluence]CVE-2019-3396`（423 事件 / 1543s / 未解）：
第 66 步读出 `Confluence 6.10.2`，第 51 步已写好 Velocity `Runtime` payload，
然后 ~90 步去修安装向导（`/status` 返回 `503 HoldingUntilStarted`）。

文献对齐（结论一致，不是本地怪癖）：
- **arXiv 2609.10780**：给两个系统都加 coverage-memory 层，**成绩都没提升**；
  归因为 "planning and commitment" —— "held the evidence... never turned it
  into a concrete exploitation hypothesis"。
- **arXiv 2602.17622 (Excalibur)**：Type A=工具/prompt（可工程修）；Type B=规划与
  状态管理，根因缺实时难度估计，**largely invariant to the underlying LLM**。
  TDA(4 维) + EGATS，CTF 91% 完成率，+39~49%，**没换模型**。
- **arXiv 2607.02605**（81 篇综述）：四阶段演化，RLVR 是前沿，但
  "高质量训练数据稀缺" 是三大开放挑战之一。

---

## 本次已完成的代码改动（全部已通过 import 自检）

1. **`backend/app/services/known_exploit.py`**
   新增 `FIRST_MOVE_DISCIPLINE`（1247 字符，3 条约束）：
   - 拿到 product+version / CVE id 立刻写 `notes/hypothesis.md` 并去打那个端点
     （明确写了 Confluence 6.10.2 + CVE-2019-3396 → `POST /rest/tinymce/1/macro/preview`）
   - 靶机自身的安装状态不是攻击目标（`503` / `HoldingUntilStarted` /
     `/setup/*` 的 500 都不该由 agent 去"完成"）
   - 死路是切换信号不是重试（连续 ~8 步无新 fact ⇒ 换技法类别，不是换 payload 变体）

2. **`backend/app/core/config.py`**
   新增 `first_move_discipline: bool = True`（A/B 开关，`APP_FIRST_MOVE_DISCIPLINE=0` 关闭）

3. **`backend/app/services/workspace.py`**
   `agents_md` 拼接顺序改为：
   `RUN_POLICY + FIRST_MOVE_DISCIPLINE + RECON_DISCIPLINE + hints + known_exploit`
   新块落在 **第 5-8 行**；整个 AGENTS.md 共 14 行 ⇒ `head -50` 必然覆盖。
   （记忆：9 种读法仅 6 种能读到头部，`tail -50` 和 `rg` 读不到，所以必须放最前。）

4. **`scripts/measure_first_exploit.py`**（新增，**尚未实跑过**）
   指标：首次"利用形态"命令出现的步序号 + `installer_before_exploit`（是否先去装环境）
   + `gap_after_first_exploit`（首次利用到第二次利用的间隔，衡量"打一枪就跑")。
   有 `_NOT_EXPLOIT` 负向表把 `/setup/` `/bootstrap/` `/status` 排除，避免把 Confluence
   那种行为算成"已提交"。

已验证：
```
import OK | first_move_discipline = True | block chars = 1247
measure_first_exploit.py OK (ast.parse)
AGENTS.md ON: 14 行, head-50 覆盖=是, 约束 6 条
AGENTS.md OFF: 9 行, head-50 覆盖=否, 约束 3 条
```

---

## 下一步（按顺序）

1. **跑 `scripts/measure_first_exploit.py` 的历史基线**
   ```bash
   cd "D:/desktop/毕业设计/C7F-W3B-Q1LL3R"
   env -u PYTHONPATH -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
     ./.venv3.11/Scripts/python.exe scripts/measure_first_exploit.py \
     --limit 200 --json-out data/audit/first-exploit-baseline.json
   ```
   它可能一次就能跑通，也可能需要调 `_EXPLOIT_SHAPES`（先用 `--run 5cb19393` 单条校准，
   确认那条的 `first_exploit_step` 落在 51 附近、`installer_before_exploit` 的判定合理）。

2. **补测试**（`backend/tests/`）：锁住
   - `first_move_discipline=True/False` 时 AGENTS.md 头部内容差异
   - 分类函数对 Confluence 那类命令判成 `installer` 而不是 `exploit`
   - 新块必须出现在前 50 行（防以后有人往前面插东西把它挤出去）

3. **跑全量回归**（基线 **2185 passed**，本次改动后应 ≥ 2185 + 新增）

4. **A/B 实验**（约 10 题 × 2 臂 × 25min）
   - 挑题目：优先已解出过的题（有 ground truth），避开已知坏靶机
   - 对照臂：`APP_FIRST_MOVE_DISCIPLINE=0` 起后端
   - 判定：看 `first_exploit_step` 中位数是否左移 + `installer_before_exploit` 占比是否下降
   - ⚠️ **跑 Run 期间不要改后端代码** —— 昨天就是因为改 `official_worker.py` 触发重载，
     把 Run 3 打断成 `CANCELLED`

---

## 遗留项（不在本次范围，别忘了）

- **15% 的长跑是靶机自己坏了**：39 条长跑里 6 条出现 setup 向导 500 / `HoldingUntilStarted`
  （`db798b52` Nexus、`94200708` Jenkins、`5cb19393` Confluence、`79ca6b1a` ActiveMQ、
  `f425b23a` PHP、`6246355f` 996game）。这类应自动标 broken 并跳过，别计入 agent 能力。
- **B1 仍未验证**：`fresh_reproduction_verified` 全库 = 0。下次任何题 accepted 会自动触发。
- **`ARENA_WORKER_ROUND_SECONDS=1500` 不改**（数据：cap1500 保住 163/169，放宽最多 +6）。

---

## 环境提醒（回家冷启动）

```bash
# MySQL 常自己 Exited
docker start c7f-w3b-q1ll3r-mysql-1
# DB 端口是 3307；asyncmy 必须写 127.0.0.1，写 localhost 会走 IPv6 ::1 超时
# Settings 前缀是 APP_ （所以覆盖数据库是 APP_DATABASE_URL）
# 起后端（无 --reload，改码必重启）
cd "D:/desktop/毕业设计/C7F-W3B-Q1LL3R" && env -u PYTHONPATH BACKEND_PORT_OVERRIDE=8000 bash scripts/_run_backend.sh
```

后端当前是起的（17:26 时 `backend:200`），关机后自然消失。
