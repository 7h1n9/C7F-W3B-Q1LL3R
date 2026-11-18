# 2026-11-08 方向一：vulhub 提取器标题正则修复

## 一、最初的假设是错的

我以为「6 个 CVE 在语料里缺失，补上语料就能进 FIRST ACTION 通道」。查完发现**根本不是语料缺失，是提取器有 bug**。

那 6 个 CVE 的 README 全都在 `.probe/vulhub-dump/` 里，只是提取器没读出来。

## 二、三个真实 bug

### Bug 1：关键词必须独占标题

`_REPRO_SECTION` 要求关键词以 `\s*$` 结尾，即整个标题**只有**这个关键词。所以：

```
## 漏洞复现              ✅ 匹配
## Vulnerability Reproduction   ❌ 不匹配（多了 "Vulnerability" 前缀）
## Vulnerability Detail         ✅（原有 vulnab\w+\s+detail 分支救回来了）
```

影响：Apache CVE-2017-15715、GhostScript CVE-2019-6116、Drupal CVE-2019-6341 等英文 README 全部 0 段落。

**修复**：加「修饰词 + 关键词」分支，允许 `Vulnerability Reproduction` / `Exploitation Details` / `漏洞复现步骤` 这类形状，但要求关键词仍在末尾（否则文档大标题会被误判）。

### Bug 2（更隐蔽）：中文版 README 用的是另一个词

`_pick_readme` **优先选 `README.zh-cn.md`**（注释说明：两语言都读会让每个载荷重复一次）。但中文版 Samba CVE-2017-7494 的标题是：

```
## 测试过程
```

`测试过程` 不在正则里。所以**英文版能提取、中文版不能，而代码只读中文版**——Samba 的载荷就这样消失了。

我此前把它记成「围栏感知的 heading 判定」问题，那个已经修过，但没解决问题，因为还有这一层。

**修复**：加 `测试过程`、`复现漏洞`（倒装词序，另有 6 个 CVE 用）。

### Bug 3：正则覆盖范围统计

我先扫了 328 个中文 README，确认影响面：

| 标题 | 出现次数 | 状态 |
|---|---|---|
| `漏洞复现` | 295 | 已匹配 |
| `漏洞利用` | 13 | 已匹配 |
| `Exploit` | 3 | 已匹配 |
| `漏洞详情` | 1 | 已匹配 |
| `测试过程` | 1 | **本次修复** |
| `复现漏洞` | 2 | **本次修复** |
| `测试环境搭建` | 10 | **正确跳过**（这是环境搭建） |
| `测试环境` | 9 | **正确跳过** |

**关键判断**：`测试环境搭建` / `测试环境` 加起来 19 次，比真正要修的还多。如果为了「多提取」把「测试」加进关键词，会把 `docker compose up -d` 全部当成载荷——这正是语料曾被污染的方式。所以只精确加了 `测试过程`（1 次）而不是宽泛的 `测试`。

## 三、修完的净增只有 1 条，而且这是对的

修完正则，跑全量提取：

```
扫描 248 个 CVE 目录 → 提取 259 条载荷
```

对比修复前的候选文件：**新增 1 条**（`vulhub:mysql:cve-2012-2122`）。

那 20 个「从 0 段落变成有段落」的 CVE，逐个查了为什么仍不产出：

| CVE | 段落 | 围栏块 | 结论 |
|---|---|---|---|
| `httpd/CVE-2017-15715` | 1 | **0** | README 全在讲「文件名加 `\x0A`」，无任何 HTTP 请求示例，行内正则也抓不到 ⇒ **本来就不该提取** |
| `ghostscript/CVE-2019-6116` | 1 | 1（`docker run ... identify /tmp/poc.png`）| 本地测试命令，不是打靶机的请求 ⇒ 不该提取 |
| `drupal/CVE-2019-6341` | 1 | 1（`php cve-2019-6341-exp.php ...`）| 依赖 Worker 容器里没有的外部脚本 ⇒ 不该提取 |
| `samba/CVE-2017-7494` | 1 | 1（`./exploit.py -t your-ip ...`）| 同上，`exploit.py` 不在容器里 ⇒ 不该提取 |
| `django/CVE-2017-12794` | 0 | 0 | 是 **XSS**，浏览器侧，Worker 发不了 ⇒ 不该提取 |

**这些「失败」全部是正确的过滤。** 一个提取器把「本地 docker 命令」当成「打靶机的载荷」，比漏掉一条危害大得多——它会让 Worker 在容器里 `docker run` 一个不存在的镜像。

## 四、关于 367 vs 259 这个数字

我一开始以为提取器退化了（之前跑出 367 条，现在 259 条）。查清了：

**367 是 bug 状态下的产出。** `_RUNNABLE` 正则尾部有个空分支 `|`，让 `_RUNNABLE.search("anything")` 永远为 True——「这是否可发送」的闸门完全失效。代码注释记录了实测结果：

> measured on the 248-README corpus, 132 blocks passed *only* because of the empty branch, including Nginx `location / {`, Samba `[global]`, `#EXTM3U` playlists and C source.

这个已经修掉了。**259 才是正确数字**，抽检 6 条确认质量：

```
vulhub:elfinder:cve-2021-32682:2   GET http://{{target_url}}/files/shell.php HTTP/1.1
vulhub:coldfusion:cve-2017-3066:2  POST /flex2gateway/amf HTTP/1.1
vulhub:confluence:cve-2021-26084:3 GET /pages/createpage.action?...%5cu0027%2b%7b233*233%7d
vulhub:couchdb:cve-2017-12636:5    curl -X PUT http://vulhub:vulhub@{{target_url}}/vultest/_design/vul
```

都是真实可发送的请求。

## 五、一个仍然存在的可执行性问题

抽检发现部分载荷依赖 Worker 容器里**没有的工具**：

```
java -jar ysoserial.jar CommonsBeanutils1 "touch /tmp/success" | base64
java -jar ysoserial-master-30099844c6-1.jar CommonsBeanutils1 ... > poc.ser
```

`ysoserial` 不在镜像里（盘点过：有 sqlmap/ffuf/gobuster/hydra/commix/nc/expect/tmux/jq，没有 ysoserial）。

这类载荷会被投递进 FIRST ACTION，但 Worker 执行时会报 `jar not found`。**这是 `verified_solve` 之外的另一层「纸面能力」**——载荷语法正确但环境不支持。

没在这次修，因为需要先统计影响面（有多少条依赖容器外的工具）。

## 六、产出

**代码改动**：`scripts/extract_vulhub_payloads.py` 的 `_REPRO_SECTION` 正则。

**新增测试**：`backend/tests/test_repro_heading.py`（21 条）
- 12 个必须匹配的标题（含 `测试过程`、`复现漏洞`、`Vulnerability Reproduction`）
- 9 个必须拒绝的标题（含 `测试环境搭建`、文档大标题）

每个测试的 docstring 都写了「这个 bug 曾造成什么损失」，避免以后有人为了多提取而放宽判据。

**回归**：`2371 passed`，零失败。

**语料**：候选文件 259 条（未合并进主库）。主库 `vulhub.jsonl` 仍是 478 条未变——净增只有 1 条，不值得为它引入合并风险。

## 七、诚实的结论

方向一的原始目标「补 6 个缺失 CVE 的语料」**基本失败了**：

| CVE | 结果 |
|---|---|
| `CVE-2017-15715`（HTTPD）| README 无 HTTP 请求示例，无法提取 |
| `CVE-2019-6116`（GhostScript）| 只有本地 docker 命令 |
| `CVE-2019-6341`（Drupal）| XSS + 外部脚本 |
| `CVE-2017-12794`（Django）| XSS（浏览器侧）|
| `CVE-2020-11989`（Shiro）| 语料已有 |
| `CVE-2020-9484`（Tomcat）| 源目录和语料都没有 |

**6 个里 0 个能靠改正则补上。** 这 6 道题的缺口不是提取器能解决的——它们缺的是「非请求型载荷」（XSS、本地脚本、编译器绕过）。

真正的收获是**修掉了三个真 bug**，它们影响的是语料库整体的正确性，而不只是这 6 个 CVE：

1. 英文 `## Vulnerability Reproduction` 标题被忽略
2. 中文 `## 测试过程` 被忽略（且代码只读中文版）
3. `## 复现漏洞` 倒装词序被忽略

这三处如果不修，以后每次重新提取都会漏掉同一批 CVE，而且**不会有人发现**——因为没有报错，只是条数少了。

这也说明方向一的天花板本来就很低（估 5%）。真正该做的是方向三（FA 采纳率），那个子 agent 已经跑出结论了。
