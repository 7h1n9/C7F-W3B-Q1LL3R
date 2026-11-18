# 2026-11-04 调控模型渠道实测结论

时段：10:30 – 15:30
背景：用户补充了中转站渠道，要求继续 A1（用调控模型给 unknown 命令打技法标签）

---

## 〇、结论已更新（14:30 之后）

**`deepseek-v4.1-flash` 完全可用，8/8 准确、0 条需要救回。**

前一版结论（「中转站只能做 glm，62.5% 不可用」）是**我的排查方法有错**：

- 我用 `claude` 那条配置的 key 去查 `/v1/models`，看到只有 glm 就下了结论；
- 但 `sub.ap-sub.top` 是**同一上游、按 key 分组**，不同 key 对应不同模型组；
- 更糟的是，我在 03:05 批量改模型名时把 `Codex-deepseek-v4.1-flash` 的 **key 也覆盖成了 glm 那把**，于是它「看起来」也只能跑 glm。

### 修正后的实测对比

| 模型 | 准确率 | 干净 JSON | 备注 |
|---|---|---|---|
| `glm-5.3` | — | 0% | 完全无视 `response_format`，返回 Markdown 散文 |
| `glm-5.3-flash` | 5/8 (62.5%) | 0% | 用自造字段名（`classification`/`category`），需别名救回 |
| **`deepseek-v4.1-flash`** | **8/8 (100%)** | **100%** | 30 条试批零失败、全部 `salvaged=0`、`why` 全有内容 |

`deepseek-v4.1-flash` 的 `why` 质量也明显更高，例如：

```
lfi.wrapper  0.6  XXE with file:// DTD to Solr XML parser
rce.deser    0.6  base64 serialized PHP payload, deserialization
ssti.eval    0.6  Elasticsearch script_fields executing arbitrary script
```

### 配置恢复（我造成的破坏，已修复）

| 配置 | 我改成 | 正确值 | 状态 |
|---|---|---|---|
| `Codex-deepseek-v4.1-flash` (`7dc8b94a`) | `glm-5.3-flash` + glm 的 key | `deepseek-v4.1-flash` + 原始 key | ✅ 已从 `.probe/codex_cfg_backup.json` 恢复 |
| `claude` (`c402b884`) | `glm-5.3-flash` | `glm-5.3` | ✅ 已恢复 |

**教训**：同一中转站的不同 key 属于不同模型分组，`/v1/models` 必须**逐把 key 查**；批量改 `model_name` 时绝不能连带改 `encrypted_api_key`。

---

## 〇之二、跑批结果（截至 17:06）

```
两轮请求 2220 条 → 成功 1847 条（83%）
salvaged = 0        ← 全部是干净 JSON
why 非空 = 1847     ← 全部带理由
```

第二轮（16:44–17:06）220 条请求成功 190 条，成功率 83%，与第一轮一致。

失败 373 条集中在 59 个「整批 5 条全失败」的突发窗口，随后自动恢复——中转站限流，不是模型问题。失败不写缓存，重跑即免费补齐。

标签分布（Top 10）：

```
unknown 580 · recon.dir 231 · recon.source 171 · lfi.wrapper 125
rce.deser 124 · ssti.eval 92 · ssrf.internal 61 · sqli.union 56
rce.ognl 49 · sqli.boolean 48
```

### 对证据层的实际提升

| 指标 | 纯正则 | 正则 + LLM | 变化 |
|---|---|---|---|
| 命令覆盖 | 6,923 / 42,512 (16%) | 12,936 / 42,512 (**30%**) | +87% |
| 有效技法（≥2 accepted run） | 6 | **9** | +50% |

新增的 3 条是正则完全认不出的：

| 技法 | fact | dead | 命中率 | 说明 |
|---|---|---|---|---|
| `rce.deser` | 4 | 2 | **67%** | 序列化 payload（base64 PHP 对象等），正则无对应特征 |
| `ssti.eval` | 2 | 3 | 40% | 模板表达式求值 |
| `sqli.union` | 3 | 4 | 43% | 从 2 提升到 3 |

### 生产排序的变化

```
旧（纯正则证据）：Command injection and RCE · SQL injection · Local file inclusion
新（+LLM 证据）：  PHP deserialization · Command injection and RCE · Server-side template injection
```

`PHP deserialization` 从 0 runs 变成 5 runs/60%（LLM 认出了正则漏掉的序列化命令），直接进 top1。标题命中仍然优先：`EasySQL` → SQLi 第 1，`Upload` → Upload 第 1。

### 泛化性（诚实结论）

train→test 迁移从 **1/6 提升到 2/9**（`rce.ognl`、`lfi.wrapper`）。有改善，但 **test 只有 12 个 accepted Run，这个数字本身不可靠**，不能据此宣称泛化提升。要判定必须等更多 test 样本。

---


## 一、旧结论（已作废，保留作为排查教训）

~~`sub.ap-sub.top` 中转站可以做「连通」，但不能做「结构化分类」。~~

作废原因见第〇节：我用错了 key 就下了结论。以下是当时基于 `glm` 系列观测到的现象，**对 glm 仍然成立，对 deepseek 不成立**。

## 二、具体失败模式（都已写进代码注释，避免重犯）

### 1. `glm-5.3` 完全不遵守输出格式

同一命令连续三次请求，返回三段不同的 Markdown 散文：

```
**Classification:** Read a file / data access
**Classification: Reconnaissance / Service Enumeration**
This command attempts a **Local File Inclusion (LFI)** test ...
```

即使带 `response_format={"type":"json_object"}` 也一样。

### 2. `glm-5.3-flash` 遵守格式，但用**自己的字段名**

```
{"classification": "reconnaissance", "confidence": 0.75, "reason": "..."}
{"category": "reconnaissance", "tool": "curl", ...}
{"classification": "reconnaissance", ...}
```

字段名在三次调用里都不一样，说明它没有真正理解 schema。

### 3. 长命令导致长时间思考直至超时

439 字符的 inline python 脚本 → **超过 150 秒仍无响应**；同批次 100 字符的 curl 只要 6 秒。
即使把发送长度截到 400 字符，该条仍超时——是命令形态触发的，不是长度。

### 4. 并发敏感

并发 4 时成功率从 100% 掉到 27%（213/310 条 `unparseable_json`）；并发 1 也不稳定。

---

## 三、本轮代码改动（都是为适配这个渠道，且都可回退）

`backend/app/training/technique_classifier.py`：

| 改动 | 原因 |
|---|---|
| `max_tokens` 160 → 1200 | 模型先输出 `reasoning_content`，160 时预算被吃光，`content` 为空、`finish_reason=length` |
| 加 `response_format={"type":"json_object"}` | flash 模型遵守该参数；glm-5.3 不遵守 |
| 加 `truncated_before_answer` 错误码 | 区分「预算不够」与「答非所问」，否则诊断时看同一个错误 |
| 用户消息加 JSON 约束句 | 只在 system 里说无效，加在 user 里后同一命令连续三次返回 JSON |
| 字段别名读取（`classification`/`category`/…） | 模型用自己的字段名 |
| `_ALIAS_LABELS` 词表归一化（40 条） | 模型给 `reconnaissance` 这类自由词，需映射到封闭词表 |
| `_salvage_label` 散文抽取 | 散文里写着 "Local File Inclusion"，信息是对的只是格式不对 |
| `salvaged` 标记 | **关键**：区分「干净 JSON」与「救回的结果」。实测 28 条看起来干净、实际全部经过别名 |
| `unknown` 与真实标签共存时丢弃 `unknown` | 观察到 `["unknown","ssrf.internal"]` 这种自相矛盾组合 |
| 缺 confidence 记为 0.3（显式 0 保留） | 缺失 ≠ 断言为零，0.0 会拉低下游权重 |
| 硬超时 `asyncio.wait_for` | httpx 的 read timeout 只在字节间触发，慢速流式可绕过 |
| `COMMAND_SEND_CHARS=400` | 长命令触发长思考 |
| 重试 2 次 → 1 次 | 散文已能救回，多试只是浪费时间 |

`scripts/classify_unknown_commands.py`：

- `FLUSH_EVERY=5`（原 25）：25 条一批时 15 分钟不落盘，中断即全丢；
- **失败不写缓存**：失败记录会被下次读成「已分类」，永久阻塞该命令（实测 245 条失败记录让缓存看着完整、实际只有 26% 数据）；
- 默认超时 150 → 45 秒，快速失败；
- 进度行加 `flush=True`。

测试：`tests/test_training_technique_classifier.py` **51 条**，覆盖别名归一化、salvaged 标记、散文抽取、超时、重试边界。

---

## 四、清理

`data/training/technique-cache/` 已清空。里面那 28 条是旧解析器产物（无 `salvaged` 标记、confidence 全 0.3、`ls -la` 被判 `recon.source`），不可信，留着会污染后续证据。

---

## 五、下一步的选择（需要决定）

| 选项 | 说明 | 我的判断 |
|---|---|---|
| A. 换模型 | 用真正支持结构化输出的模型（需先确认渠道里有） | **推荐**。62.5% 准确率是模型能力问题，不是提示词问题 |
| B. 只保留高置信度 | 用 confidence 过滤，只留能自证的 | 不可行：全部 0.3，没有区分度 |
| C. 双模型投票 | 同一命令问两次，一致才采纳 | 成本翻倍且渠道不稳定，性价比低 |
| D. 放弃 LLM 分类 | 回到纯正则词表（17% 覆盖） | 保守但可靠，A3 证据排序已能工作 |
| E. 人工标注小样本 | 手工标 200-300 条高质量数据 | 最可靠，但耗时 |

**建议**：先确认渠道里有没有真正支持 `json_schema` 的模型（如 Qwen、DeepSeek 官方）。如果只有 glm 系列，走 D——A3 已经用现有 6 条证据跑通了排序，不需要更多标注也能工作。

---

## 六、A3 不受影响

上一轮完成的证据排序（`top3: RCE/SQLi/LFI`）依赖的是 6 条已有证据，**不依赖本轮 LLM 标注**。所以即使 A1 完全放弃，A3 仍然有效。

---

## 七、回归测试

```
576 passed（training / technique / knowledge / arena / worker）
51 passed（technique_classifier 单文件）
```

---

## 〇之三、并行跑批实测（21:00–22:00）

用户要求尝试并行。结论：**并行能做，但收益不如预期，因为瓶颈是渠道限流而不是本地算力。**

### 并发上限实测（30 条简单命令，短突发）

| 并发 | 成功率 | 吞吐 |
|---|---|---|
| 3 | 30/30 | 54/min |
| 5 | 30/30 | 76/min |
| 6 | 24/24 | 99/min |
| 12 | 24/24 | 185/min |
| 16 | 40/40 | **214/min** |
| 24 | 20/40（429） | 169/min |

短突发能到 214/min，但**长跑完全不同**：并发 8 连跑 5 分钟后成功率掉到 66%，并发 18（3 进程 × 6）时出现 795 个整批全失败。

### 并行实现（已完成，可复用）

1. **缓存加写锁**（`TechniqueCache._locked`）：Windows 没有 `fcntl`，用 `O_CREAT|O_EXCL` 标记文件做互斥；标记载入 5 秒超时后强制继续（宁可写一行坏行——`load()` 会跳过不可解析行——也不能让整轮卡死）。
   - 初版用共享锁文件 + `fcntl`，Windows 上两个线程同时 `open(...,"a+b")` 直接 `PermissionError`，被并发测试当场抓住。
2. **`--shard i/n` 分片**：worker 共享缓存时都从同一位置开始，实测前几分钟完全重叠。改成轮转分配后各跑各的第 i/n 条。
3. 测试：`test_two_writers_do_not_corrupt_the_cache` 用两个线程各写 40 条，验证 80 条全部落盘且每行都是合法 JSON。

### 实测吞吐

| 配置 | 有效吞吐 |
|---|---|
| 1 进程 × 并发 6 | ~70/min（83% 成功率） |
| 3 进程 × 并发 6 | 前 2 分钟 460/min，之后崩到 ~100/min（大量 429） |
| **1 进程 × 并发 5（当前）** | ~65/min，成功率回到 100% 的稳定区间 |

**结论**：这个渠道的长跑安全并发约 5-8。多进程不会更快，只会更快撞上限。真正的加速要等渠道配额提升，或换一条渠道。

### 覆盖率的边际收益递减

| 标注条数 | 命令覆盖 | 有效技法（≥2 accepted run） |
|---|---|---|
| 0（纯正则） | 16% | 6 |
| 1,847 | 30% | 9 |
| 4,576 | 43% | 9 |
| **7,855** | **59%** | **9** |

覆盖率还在涨，但**有效技法停在 9 条不再动**——瓶颈已经不是标注数量，而是**只有 81 个 accepted train Run**。继续堆标注只能让已有技法更稳，不会长出新的技法维度。这一点决定了"要不要跑完剩下 1.3 万条"的答案：**收益很低**。
