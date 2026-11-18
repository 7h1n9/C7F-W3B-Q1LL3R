# A 方案：从已解 Run 反推 payload（2026-11-05）

用户选择 **D（A 先做，通了再拓 B/C）+ E（先查 85% 失败原因）**。
E 已完成（`docs/notes/2026-11/2026-11-05-failure-attribution.md`），本文件是 A 的落地。

---

## 一、为什么要从已解 Run 反推

E 的诊断结论：未解 Run **没有系统故障**（243/244 无错误码）、**在干活**（命令中位 87）、
**目标在正常响应**（89% 的 Run 失败返回占比 <60%），却只沉淀 **4 条 fact**。

根因是手里没有可套用的具体 payload：
- `payload-corpus.jsonl` 只有 **6 条**，全 `unverified`
- `cve-playbook.jsonl` 只有 **20 条**
- worker 实际只能读通用 vulhub README，有 4 次甚至去读靶机的 `docker-compose.yml` 反推环境

从已解 Run 反推有三个别的方法没有的优点：
1. **与靶机同源**：payload 是在同一套 `direct-ctf2` 环境里真的打通的；
2. **含失败边界**：同一条 payload 的失败记录就在同一个 `run_events` 里；
3. **零外部依赖**：不依赖网络，不涉及爬取合规。

## 二、怎么定位"决定性命令"

不是最后一条，也不是打印 flag 的那条。flag 常在利用之后好几步才出现。
做法：**flag 首次出现的 tool_result 之前，最近的利用形态命令**（向前最多找 25 步）。

例（`0935613b` `[De1CTF 2019]SSRF Me`）：
- flag 在 seq 73 被读到
- 决定性命令是 seq 73 那条 `geneSign` 签名伪造 + `action=readscan` 重放

## 三、结果

```
扫描 147 个已解 Run → 提取 38 条候选 payload
  跳过：flag 不在 worker 输出里 30 条
        flag 前找不到利用形态命令 33 条
        不是 payload（查用法/帮助）5 条
```

技法分布：

| 技法 | 条数 |
|---|---|
| unknown | 13 |
| ssti | 5 |
| lfi | 4 |
| rce / path-traversal / sqli | 各 3 |
| graph-query-injection | 2 |
| credential-abuse / image-parser-rce / arbitrary-file-read / file-upload / xxe | 各 1 |

占位符：`target_url` 29 条、`scratch` 3 条、`ip` 1 条、无占位符 6 条。

## 四、三道安全闸（都踩过坑，都已修）

### 1. 脱敏：真实主机名必须裸匹配

初版正则要求 `https?://` 前缀，结果 `root@direct-ctf2.dasctf.com`（ssh 命令）
**没被替换**，真实主机名进了候选文件。

已改为**不带 scheme 也匹配**，并补了外网 IP（曾漏 `117.21.200.176`）、
API key 的规则。现在 6 类用例全部通过，候选文件全量扫描 **0 泄漏**。

### 2. 非 payload 必须剔除

初版 43 条里 **9 条**是 `ysoserial.GeneratePayload 2>&1 | head -40`、`--help`、`which`。
这类条目匹配了"利用形态"只因为出现了工具名，实际没有任何可重放内容。
一个塞满这种条目的审核队列会训练人闭眼放行，等于白做。

### 3. 技法标签：payload 形态优先于题目名

`[De1CTF]SSRF Me` 实际是靠 **md5 签名伪造**解的，不是 SSRF。
若按题目名归到 `ssrf`，这条 payload 永远不会被 `ssrf` 之外的检索命中。
已改为**形态优先、题目名只兜底**，并从兜底列表里删掉 `ssrf`
（该题给了标题却给不出对应 payload，兜底反而有害）。

## 五、`unknown` 还有 13 条（43%）

原因是这些 payload 形态尚未被归类（如 PIL 的 EPS 解析、jenkins-cli 读文件）。
**这 13 条保留在队列里等人工标注**，脚本不猜。

## 六、为什么放在 `data/knowledge/candidates/`

`known_exploit.py:1449` 会 `glob("*.jsonl")` 扫描 `data/knowledge/`，
把候选文件平铺在那里会导致 **13 条测试失败**（`KnowledgeItem` schema 校验 11 处错误）。
`known_exploit.py:304` 的注释早就写过这条规则（"One level down on purpose"），
我这次是重犯了同一个错误。已移到 `candidates/` 子目录。

## 七、下一步

1. **人工审核这 38 条**：确认 payload 真实、改对 `technique`、补齐 `preconditions`，
   然后 `trust_level` 从 `candidate` 升到 `verified` 才合并进 `payload-corpus.jsonl`
2. 审核过的再跑一遍边跑边评估（不要一次全量灌进去，避免把错误固化）
3. B/C 方案（公开 CVE 仓库、历史脚本聚集）在 A 的流程跑通后再拓

## 八、代码

- `scripts/extract_solved_payloads.py`（新增，只读数据库）
- `backend/tests/test_extract_solved_payloads.py`（13 条，含"候选文件不得含真实主机/flag"的成品检查）
- 产物：`data/knowledge/candidates/payload-candidates.jsonl`（38 条，`trust_level=candidate`）

全量回归 **2214 passed**（基线 2201 + 13）。
