# 公开 PoC 联查：可用性实测与召回修复

**日期**：2026-10-19
**范围**：`backend/app/solver/muteki/worker/public_poc.py`、`ranges/vulhub-tomcat-cve-2017-12615/`

## 结论

联查**可用**。NVD 与 GitHub 都能从本机直连，一次查询 4 秒返回 CVE 元数据与 PoC 仓库。

但"能连通"不等于"能用"：初版有三个召回缺陷，在真实查询里分别是 0 结果、
2000 年的老 CVE、和 403 限速——三种表现都长得像"这个产品没有漏洞"，会把模型引向错误结论。
三个都已修，并且是在真实靶场上发现的，不是推演出来的。

## 一、实测

```
lookup_public_poc(keyword="apache log4j2 jndi")   4.2s
  CVE-2021-44228  CVSS 10.0  CRITICAL
  CVE-2021-44832  CVSS 6.6   MEDIUM
  PoC  fullhunt/log4j-scan     3428 stars
  PoC  kozmer/log4j-shell-poc  1847 stars
```

NVD 与 GitHub 均走 `trust_env=True`，本机 7897 代理下可达。

## 二、三个召回缺陷

### 1. 长句 → 0 结果

`keywordSearch` 是**对 token 做 AND**。模型把一整句描述当关键词（这是最自然的写法）时：

```
"apache tomcat 8.5.19 put jsp arbitrary file write"  →  total 0
```

**修法**：`_keyword_variants()` 给出阶梯查询，最多三档。

### 2. 带版本号 → 0 结果

这一条最反直觉。NVD 把受影响版本范围存在 **CPE** 里，不在描述文本里：

```
"tomcat 8.5.19"      →  total 0        ← 模型最可能输入的形态
"tomcat"             →  total 421
```

所以第二档必须**主动删掉版本号**，而不是只去停用词。删掉之后 `apache tomcat 8.5.19`
的变体是 `apache tomcat`，实测 4.4s 返回 312 条。

这一点决定了工具的实用性：模型按"产品 + 版本"查询是最正确的直觉，而 NVD 的索引让这个查询恰好落空。

### 3. 宽词 → 最老的一页

NVD 按**发布时间升序**返回，`resultsPerPage` 默认只取前几条。所以：

```
"tomcat" → CVE-2000-0672, CVE-2000-0759, CVE-2000-0760   （2000 年）
```

拿 2000 年的 CVE 打一个 2017 年的靶机，比什么都不返回更糟——模型会验证失败并排除掉整个漏洞类。

**修法**：当 `totalResults` 超过一页时，用 `startIndex = total - 50` 取**末页**，再在本地按
CVSS 排序取前 `limit` 条。修后同一查询返回 2026 年 9 月的 CVE，CVSS 9.8。

**试过但没有采用的方案**：一次拉全（`resultsPerPage=2000`）。`tomcat` 的 421 条是 3.9 MB、
30 秒，而且排序后第一条是 Cisco 的一份 advisory——"tomcat" 在描述里出现的地方太多了。
成本高、结果更差，直接否掉。

### 4. 加时间窗口？不可行

NVD 的 `pubStartDate` / `pubEndDate` **最大跨度 120 天**，单独给 `pubStartDate` 直接 404。
想按年倒查只能按月分段，请求数爆炸。所以末页策略是当前 API 约束下的最优解。

### 5. 无 key 限速

NVD 无 API key 是 **5 次/30 秒**，而一次 lookup 最多发 3 个请求（原始词、放宽词、末页），
多个 Worker 并发必然撞 403——而 403 在调用侧和"没有 CVE"不可区分。

**修法**：无锁的槽位预约。每个调用方把 `_nvd_next_slot` 往前推再 sleep，实现排队，
避开 `asyncio.Lock` 跨 event loop 绑定的问题（每个 Worker 跑自己的 loop）。

## 三、vulhub 靶场

`ranges/vulhub-tomcat-cve-2017-12615/`，Apache Tomcat 8.5.19，CVE-2017-12615
（`DefaultServlet` 在 `readonly=false` 时接受 PUT 写 JSP）。

**关键事实：vulhub 官方镜像默认不复现这个漏洞**。它的 `conf/web.xml` 保持 `readonly=true`，
PUT 返回 403。所以 `conf/web.xml` 的覆盖本身就是漏洞条件，必须版本化，不能打进运行中的容器。

镜像经 `docker.1ms.run` 拉取（`registry-1.docker.io` 直连超时），`daocloud`/`xuanyuan` 均 403。

手动验证：

```
PUT /x.jsp/  →  201
GET /x.jsp   →  flag{0398a8fac1cbeebb3460fd95eeaecf46}
```

### 两轮真实 Run（A 路，deepseek-v4-flash）

| 轮次 | 联查 | 结果 | 归因 |
|---|---|---|---|
| 1 | 1 次，`apache tomcat 8.5.19` → **0 条** | UNSOLVED 517s | 联查空；Worker 自己摸到 `PUT 201` 但没读回 |
| 2 | 1 次，`Apache Tomcat 8.5.19` → **4 条 9.8** | UNSOLVED 612s | 联查已修好；Worker 写出可执行 JSP 但内容是 `out.println("PWNED123")` |

两轮都**证明链路是通的**：联查拿到了 CVE，靶机接受 PUT 并执行了 JSP。
失败点在 Worker 内部：它把"证明代码执行"当成了终点，没有用这个执行能力去读 `/flag.txt`。

靶机侧留存的物证（第一轮）：

```
mut1.jsp  265B   读 /flag.txt，GET 直接返回 flag
n20m53f9.jsp     同上，带 try/catch
og3ejvyy.jsp     同上
p5os65je.jsp     webshell，支持 ?cmd=
```

即 Worker 写下的 shell **全部有效**，只是它自己没去请求。

针对这点加了两处提示（不改判定逻辑）：

* 系统提示补充："Code execution is a means, not the goal"；
* `submit_flag` 返回 `FLAG_NOT_IN_OBSERVED_RESPONSE` 时附带可执行的下一步说明，
  而不是只回一个错误码——否则模型会原样再猜一次。

## 四、遗留

* 联查只对 **A 路**（`openai_compatible`）可用。B 路 CLI Worker 用的是容器内的离线库
  （`/home/kali/pocs/vulhub`、`PayloadsAllTheThings`、`hacktricks`），两者不是同一条通道。
* 取末页让**旧版本靶场**拿不到对应年份的 CVE（末页是最新的 50 条）。版本精确匹配
  需要 CPE 查询，未做。
* GitHub 仓库搜索经常返回空（60 次/小时无认证）；NVD 那部分不受影响。
