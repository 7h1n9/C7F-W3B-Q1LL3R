# WP 生成链路实跑验证报告

**日期**：2026-10-11
**目标题目**：Sentinel-1B:被记忆的 FLAG（模型反演 / 训练数据提取）
**新 run_id**：`95189e4b-4cbd-4907-bbb4-1f3220252909`
**验证命题**：能否用「模型读取 Worker 对话」生成完整报告

---

## 一、结论

链路**已打通**，报告质量发生质变。但过程中发现并修复了 **3 个真实缺陷**，其中 1 个
（路径未回映射）会导致整个改造在生产环境**静默失效**——对话写进了盘根，
`data/workspaces/<run>/evidence/` 永远是空的。

---

## 二、修掉的 3 个缺陷

### 缺陷 1（致命）：容器路径未回映射，对话落到盘根

| 项 | 说明 |
|---|---|
| 现象 | `evidence/cli-conversations/` 始终为空；对话实际写到 `D:\home\kali\workspace\evidence\cli-conversations\` |
| 根因 | container 后端下 `MUTEKI_WORKSPACE` 是**容器路径** `/home/kali/workspace`（`coordinator.py:1214`）。宿主机的 `Path('/home/kali/...')` 在 Windows 被重挂到当前盘根 |
| 修法 | 新增 `_conversation_host_workspace(workspace, handle)`，用 `handle.host_workspace`（真实 bind source）反向映射；`handle` 缺失时原样透传 |
| 影响面 | **不止对话**——`D:\home\kali\` 里还有 `.muteki-artifacts/`、`shared/`、`graph/`、`workers/`，时间戳最早到 **2026-09-09**，说明这是长期存在的路径映射缺陷 |

### 缺陷 2：模型输出非法转义导致整份报告丢弃

| 项 | 说明 |
|---|---|
| 现象 | `Invalid \escape: line 43 column 36`，`status=failed`，sections 全空 |
| 根因 | 对话原文满是 `\"`、`\n`、正则 `\d`；模型复述时漏转义，严格 `json.loads` 直接拒绝整份文档 |
| 修法 | 新增 `_loads_tolerant()`：严格解析 → 修复未知转义重试 → 剥离裸控制字符再重试 |

### 缺陷 3：flag 前缀被模型猜成 `flag{`

| 项 | 说明 |
|---|---|
| 现象 | repro 脚本用 `flag_prefix = "flag{"`，真实是 `CTF2{` |
| 根因 | `build_model_input` 的 challenge 段**没有 flag_pattern 字段** |
| 修法 | 注入 `challenge.flag_pattern`（Coordinator 设 `MUTEKI_FLAG_PATTERN` 用的同一来源）；prompt 加硬约束「前缀以此为准，repro 字段名必须来自对话真实响应，不得臆造」 |

---

## 三、抓到什么对话

4 份真实转录（sink 多轮工作正常）：

| Worker | 引擎/模型 | 大小 | 结果 |
|---|---|---|---|
| worker-2 | claude / glm-5.3 | 37 KB | **完整跑完一轮**（8 turns，56s），产出 2 条 VERIFIED_FACT + 1 条 DEADEND |
| worker-1 | codex | 26 KB | `turn.failed` — 403 `unsupported_country_region_territory` |
| worker-1 二轮 | codex | 27 KB | 同上（23 次 request_forbidden） |
| worker-3 | codex | 25 KB | 同上 |

**关键情报（全来自对话，不在黑板）**：

```
context "CTF2"  → top-1 'X' p=0.88 (蜜罐强度) ；真实 '{' 仅 p=0.034  ← 非 top-1
context "TF2{"  → 'd' p=0.529 (疑似真实数据路径)
结论：最高概率路径不一定是真实数据，需用 /submit 的 canary 判定剪枝
```

---

## 四、报告质量对比

同一道题，改造前后：

| 章节 | 改造前 | 改造后 |
|---|---|---|
| 攻击链 | `自动化工具调用` | 8 步：`context_len=4` / `topk=3` / `vocab=85` / `CTF2{` 前缀 / 蜜罐概率对比 / beam+剪枝策略 |
| 执行过程 | 一个裸 dict | 8 步，每步标注是哪个 worker 做的（claude 侦察 / codex 失败 / coordinator 分配） |
| 修复建议 | `实施参数化查询`（SQL 模板） | 6 条模型反演专项：DP-SGD、训练语料去重、限 top-1、去掉 canary 三态、限速、蜜罐随机化 |
| 可复现方式 | `当前没有足够的已验证 HTTP 请求` | 完整 Python 脚本，前缀 `CTF2{` 正确，接口路径来自对话 |
| 诚实性 | 谎报成功 | 明写「codex 因区域限制未执行任何请求」「run 在 343s 被取消，未拿到 flag」 |

模型：kimi-k3。

---

## 五、测试

- `tests/test_muteki_llm_report.py`：7 → **9 项全绿**
  - 新增：路径回映射（含 `/home/kali/workspaceX` 前缀伪装不误映射）
  - 新增：模型转义容错（`\d` / `\S` 等非法转义 + 代码围栏）
- 相关回归：**378 passed**
- `test_muteki_coordinator.py::...reporting_grace`：批量跑挂 / 单跑过 → **并发时序 flaky，与本次改动无关**

---

## 六、遗留与建议

1. **codex 引擎当前不可用**（地区限制 403）。本轮 3/4 worker 是 codex，全部空转。
   若要靠 codex 跑通，需要给它配代理。
2. **`D:\home\kali\`（856K / 260 文件）是长期误落盘目录**，最早 2026-09-09。
   它是路径映射缺陷的产物，但**我没有删除**——位于 D 盘根、含跨月历史数据。
   建议你确认后手动清理，或保留用于排查。
3. **repro 字段名仍需人工确认**：本轮对话里确实没有 `/predict` 的响应体（claude worker
   只做了侦察未跑解码），属输入不足。模型已正确标注「打印后确认」，未臆造。
4. **PoC 自动执行验证**仍未做（依赖的 remote runner `192.168.236.128:8091` 不可达）。
