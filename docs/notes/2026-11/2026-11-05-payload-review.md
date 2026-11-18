# 38 条候选 payload 人工审核结果（2026-11-05）

审核人：Agent（自我审核，`docs/notes/2026-11/2026-11-05-payload-harvest-a.md` 里已提示"我审我自己的产出等于没有第二双眼睛"）。

**结论：38 条里只有 1 条可用。这批候选不能合并进 live corpus。**

| 判定 | 条数 | 含义 |
|---|---|---|
| accept | **1** | 完整、独立可重放、是真实利用步骤 |
| repair | 4 | 思路对，缺前置条件，保留但不合并 |
| reject | **33** | 不是利用 / 无法重放 / 含敏感数据 |

---

## 一、最严重：3 条含真实 flag（编码形态）

| # | 题目 | 形态 |
|---|---|---|
| 21 | [BSidesCF 2020]Had a bad day | base64 |
| 27 | [ACTF2020 新生赛]Include | base64（两处） |
| 36 | [PHP]XXE | base64 |

第 27 条就是 `echo 'PD9waHAK...' | base64 -d`，解码后是：

```
<?php
echo "Can you find out the flag?";
//CTF2{9572be45-...}
```

**我上一轮说"候选文件全量扫描 0 泄漏"，那句是错的。** 我的扫描正则只查明文 `CTF2{`，而这三条是 payload 本身就在 `base64 -d` 一个含 flag 的 blob。扫描器必须**先解码再查**。

已修：`scripts/audit_payload_candidates.py` 的 `find_flag()` 会尝试 base64 / URL / hex 三种解码，14 条测试里专门有一条 `test_base64_encoded_flag_rejects` 钉住这个回归。

## 二、真正致命：25 条被采集端截断在 300 字符

根因在 vendored 上游：

```python
# backend/muteki/solver/cli_driver.py:1137
return StreamStep("tool", tool="shell", text=str(item.get("command", ""))[:300])
```

全库 22,819 条命令，**最长 332 字符，8,790 条恰好 300**。`COMMAND_LIMIT=900` 只在更下游生效，上游这个 `[:300]` 早就切完了。

后果是这批 candidate 里**没有一条长 payload 是完整的**：Struts2 的 OGNL 表达式、HugeGraph 的 gremlin 语句、pickle 脚本，全部在中间断掉。

我第一版审核脚本按"模板长度 ≥290"判断，漏掉了 25 条——因为脱敏会让模板变短，300 字符的命令清洗后可能只有 250。改成**从数据库读原始命令长度**才暴露出来。

**这是历史数据，无法回溯。** 要修只能在采集端改，但那属于 vendored 上游。

## 三、"决定性命令"不等于"利用步骤"

提取器的定位逻辑是"flag 首次出现前的最近一条利用形态命令"。这个定义有个盲区：**拿到 shell 之后回读 flag 的那条命令，也符合"利用形态"**。

| # | 实际是什么 |
|---|---|
| 12 | `curl shell.php%0a?cmd=env` —— shell 是前一步传的 |
| 19 | `/upload/phtml_t1.phtml?c=cat /flag` —— webshell 是前一步传的 |
| 35 | WordPress 404.php 主题文件里的后门 —— 前一步写入 |

这三条教不会任何东西：它们的"利用"发生在更早的步骤里，被记录下来的只是收尾。

## 四、其余 reject 的构成

- **只创建载荷、没发送**：#6 `cat > f2.jpg <<EOF` 到 `sleep 1') currentdevi` 就断了；#38 `cat > final_exploit.py <<EOF` 到 `class RCE` 就断了
- **依赖 workspace 里已不存在的脚本**：#8 依赖 `./q.sh`（`yso` 目录已不在），#29 依赖 `import q`（`q.py` 已不在）
- **heredoc 未闭合但长度不长**：#16 Web4 只有 257 字符，却在 `class RCE:` 处停住——长度检查看不见，必须做**结构检查**（数终止符）

## 五、被修掉的一个真实缺陷：29 条 payload 缺 scheme

`curl {{target_url}}/robots` 这种命令 shell 直接跑不了，因为占位符只替换 hostname，不含 scheme。上一轮我写了个正则把 `http://` 从占位符前面**剥掉**，方向正好反了。

已改为：`curl`/`wget` 后面若出现裸 `{{target_url}}` 就补 `http://`；`root@{{target_url}}` 这类 host 参数形态不动。四种形态实测都正确，现在缺 scheme 的条数为 0。

## 六、唯一 accept 的那条

```
curl -s -H "Accept: ../../../../../../../../proc/self/environ{{" \
     http://{{target_url}}/robots 2>&1 | strings | grep -iE "flag"
```

Rails CVE-2019-5418。`{{` 是**漏洞语法本身**（Rails 用它触发模板渲染），不是占位符损坏——我一开始差点把它当 bug 删掉。

但这一条也只能算"半个"：它教的是"Accept 头能穿路径"，而 `/proc/self/environ{{` 这个具体路径是试出来的。

## 七、这批数据的真实价值

**作为 payload 语料：1/38，不值得合并。**

**作为证据：非常有价值。** 它证明了三件事：

1. 采集端 300 字符截断让**所有**长载荷无法复用——这是 A 方案（从已解 Run 反推 payload）的**结构性障碍**，不是质量问题
2. 提取器需要区分"利用"和"利用后回读"，否则会把收尾当技巧
3. 脱敏必须覆盖编码形态，否则真实 flag 会进磁盘

## 八、下一步的判断

A 方案原样走下去，产出率是 1/38 ≈ 2.6%。按这个比率，要凑 100 条可用 payload 得扫描 3,800 个已解 Run——而全库只有 147 个。

**所以 A 方案的天花板已经在数据里写明：它最多产出个位数条 payload。**

要继续，只有两条路：

| 路 | 前提 | 代价 |
|---|---|---|
| **先修采集截断** | 改 vendored 上游 `cli_driver.py:1137`（记忆铁律：只改读端，但这一处是采集端） | 改完只对**新** Run 生效，历史数据不会变好；需重跑题目才有样本 |
| **转 B 方案：公开 CVE 仓库** | vulhub / nuclei-templates | 不依赖历史数据质量；缺点是模板是"检测"不是"利用"，且数量大需筛选 |

我的建议是：**A 不再扩大投入，转 B**。理由不是 A 没价值，是它的天花板只有个位数条——再怎么审也审不出 100 条。

## 九、代码

- `scripts/audit_payload_candidates.py`（新增，判定 accept/repair/reject，自动从 DB 读原始长度）
- `backend/tests/test_payload_candidate_audit.py`（14 条，每条对应一个真实踩过的坑）
- `scripts/extract_solved_payloads.py`（修 scheme 补齐方向）
- 产物：`data/knowledge/candidates/review-verdict.json`（逐条判定与理由）

全量回归 **2228 passed**（2214 + 14）。
