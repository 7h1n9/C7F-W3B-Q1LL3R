# 断点：2026-11-06 17:30 暂停

回家后从这里续。**先读这一页，不要重新调研。**

---

## 一、当前状态（已确认）

| 项 | 值 |
|---|---|
| 后端 | `http://127.0.0.1:8000` → 200 |
| 前端 | `http://127.0.0.1:5173` → 200（Vite） |
| MySQL 容器 | `c7f-w3b-q1ll3r-mysql-1` Up 8 hours (healthy) |
| 全量回归 | **2304 passed**, 0 failed |
| 队列 | SOLVED 37 / UNSOLVED 93 / PENDING 0 / RUNNING 0 |
| 环境格位 | 0 / 1（空闲） |

### 重启命令（如果全停了）

```bash
# MySQL
export MSYS_NO_PATHCONV=1
docker start c7f-w3b-q1ll3r-mysql-1

# 后端（必须用后台任务方式，不能用 start-all.ps1 的 Start-Process）
cd "D:/desktop/毕业设计/C7F-W3B-Q1LL3R"
env -u PYTHONPATH BACKEND_PORT_OVERRIDE=8000 bash scripts/_run_backend.sh > .probe/backend.log 2>&1 &

# 前端（停后端时前端进程会被带走，必须单独起）
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173 > ../.probe/frontend.log 2>&1 &
```

---

## 二、今天已完成的六项改动

### 1. `DEADEND_DISCIPLINE`（prompt 层）
`known_exploit.py`，944 字符，随 checklist 注入 `AGENTS.md`。独立 marker 幂等。
> deadend 是承诺不是日志；之后不许发同一请求的变体；只剩重试就停下报负面结果。

### 2. `post_exploitation` 全量填充
`scripts/fill_post_exploitation.py`，478 条从 `[]` 填满。技法层 11 类 + 产品层覆盖（joomla/phpmyadmin/weblogic）。
> 渲染器限制：只打印前 3 条、每条 200 字符，超出静默丢弃。

### 3. `_order_by_history()`（排队器）
`arena_queue.py`。桶排序：`solved>0` 最前 → 没跑过中间 → 跑过没解出最后。不改 `priority` 列，只读历史。
> 数据依据：ACTIVE 133 道 = 从没跑过 0 / 跑过0解出 119 / 历史上解出过 14。

### 4. `stagnation_command_limit = 40`（代码计数器）
`coordinator.py`。40 条命令无新事实 → `request_convergence`。
> 为什么必需：`barren_limit=8` 数**轮次**，而三例实测全部只有 2 个 Worker 轮次；`worker_max_turns` 数 turn，一个 turn 可发多条命令。
> 阈值标定（484 Run）：触发 SOLVED 14% / UNSOLVED 34%。

### 5. `stagnation_after_deadend_limit = 12`
同上文件。见到 `DEADEND=` 标记后，预算从 40 收紧到 12。
> 对应 `1b2a8586`：第 26/36 条写 deadend，之后又跑 10 条同样的 find，`--max-time` 40→60→90。

### 6. checklist 按需裁剪（方向 A）
`execution_checklist_for(name, description, technique)`。三层：平台级永远注入 / 条件级命中才注入 / 未分类默认保留。
> Joomla 2278 字符（-52%）、PHP 3145；噪声比例 57% → 0%。原常量 `EXECUTION_CHECKLIST` 保持 4734 不变（向后兼容）。

### 7. `intel:public-poc` lane + 修掉死指令（**刚做完，最后一步**）
- 新增第 5 条 lane，`requires_protocol="chat_completions"`，让 `lookup_public_poc` 第一次真正可达
- `_SINGLE_WORKER_GOAL` 不再提 `lookup_public_poc`（CLI Worker 调不到），改成读 `/home/kali/pocs`（已实测容器内存在）
- 长度全部 ≤600（goal 414 + backfill 180 = 594；intel lane 595）

---

## 三、关键的负面结论（别再走回头路）

| 结论 | 数据 |
|---|---|
| **工具数量和解题率无关/负相关** | SOLVED 平均用 0.22 种工具，UNSOLVED 0.47 种 |
| **读更多 POC 目录与解题率无正相关** | 读0个33% / 1个45% / 2个37% / 3+个33% |
| **失败轮起步不慢** | 首次利用尝试 SOLVED 中位第5条 vs UNSOLVED 第6条 |
| **真信号是「最后认知标记后还跑多少」** | UNSOLVED 轮标记后仍跑 66 条 = 28% |
| **`lookup_public_poc` 484 Run 调用 0 次** | 注册在 `openai_compatible.py`，CLI 通道根本没有 |
| **`nc` 77% 失败** | 71 轮用 nc，55 轮伴随超时/断连；`expect` 只用了 8 轮 |

---

## 四、待做（按优先级）

### P1 — 知识库 478 条批量快速验证（用户明确要求）

**现状**：`verified_solve` 全库为 **0**。478 条 `payload_template` 能进 FIRST ACTION，但没有一条被证明能拿 flag。

**用户的要求**：拉起环境进行批量快速验证。

**已有的基础设施**（今天验证过可用）：
- 队列 + `_order_by_history` 会自动优先历史解出过的题
- `POST /api/v1/arena/queue/bulk {"action":"retry","item_ids":[...]}` → `POST /queue/start`
- 单环境格位串行，一题 8-25 分钟

**关键约束（踩过的坑，别重犯）**：
1. **已解过的题无法重跑** —— `arena_solve.py:663` `if detail.get("is_solved")` 直接 `SKIPPED_ALREADY_SOLVED`
2. **选题必须选「历史上解出过」的题**，不要选「载荷看起来干净」的（昨晚选 PHP-2012-1823，5 跑 0 解，白费 25 分钟）
3. 开跑前必查格位：`GET /api/v1/arena/connections` → `active_environments: 0`
4. `queue/start` 返回 `already_running` + `current_item_id: null` = 租约残留，**只能重启后端**
5. `bulk retry` 的 commit 有延迟，调用后立即查仍是旧状态

**规模估算**：ACTIVE 里只有 14 道历史解出过。要验证 478 条覆盖的 400 个 CVE，需要每 CVE 至少一次成功 —— 那是几百小时。**先做 14 道，拿到第一批 verified_solve 样本。**

### P2 — 方向 B：`mkt-session` 交互原语（用户要求逐步解决）

给 Worker 一个「发一条、收一条」的会话：
```
mkt-session start --host H --port P --name S
mkt-session send --name S --data '...'
mkt-session close --name S
```
底层用 `expect` 或 `socat` 常驻。

**为什么比赛前不做**：要改 worker 镜像并重建。镜像起不来就是全批失败。

**动手前先确认**：`expect` 已在镜像内（实测 `/usr/bin/expect` 存在），可能只需要加一个 wrapper 脚本而不必重建镜像 —— 走 volume 挂载或 workspace 内分发。**这条路值得先试，能避开重建镜像的风险。**

### P3 — 其他可修的点（我发现的，未见用户确认）

| 问题 | 修法 |
|---|---|
| `_RACE_LANES` 有 5 条但分派用 `index % len`，引擎少于 5 个时后面的 lane 永远轮不到 | 改成按需选 lane，而不是整除取模 |
| 条件级 checklist 的 trigger 词表是手工写的（`"php"`, `"upload"`…），可能漏 | 从 478 条语料的 technique 字段反向生成 |
| `stagnation_command_limit=40` 是全局值，未见按题区分 | 保留，先观察真实效果 |

---

## 五、外部研究备忘（已验证过的，可直接引）

1. **CTFAgent（CCS'25）**：两阶段 RAG（Understanding 用代码片段检索 / Exploiting 用模型自己的利用想法检索），Intercode-CTF 39→73/100。核心：LLM 已知道知识，缺「和场景对上」。
2. **Strix（34K stars）**：51 份 Markdown 技能包**按需 load_skill**；系统提示禁止 kitchen sink agent；「非法 final output」用工具契约强制收尾（而非 prompt 请求）。
3. **CSA 白皮书**：只给 CVE 描述 GPT-4 利用 87%，去掉描述降到 7%。⇒ CVE 描述是最大杠杆。

---

## 六、文档索引

| 文件 | 内容 |
|---|---|
| `docs/notes/2026-11/2026-11-06-verify-run-1.md` | 三轮跑题验证的完整数据 |
| `docs/notes/2026-11/2026-11-06-route-2-3.md` | 路线2+3（post_exploitation、历史排序） |
| `docs/notes/2026-11/2026-11-06-command-stagnation.md` | 停滞检测与阈值标定 |
| `docs/notes/2026-11/2026-11-06-tools-and-research.md` | 工具盘点 + 外部研究 |
| `docs/notes/2026-11/2026-11-06-direction-a-checklist-trimming.md` | checklist 裁剪 |

工作记忆：`C:/Users/7h1n.9/WorkBuddy/2026-10-12-00-45-20/.workbuddy/memory/2026-11-06.md`