# 知识语料 provenance 与发布时间核验报告

- 生成时间：2026-10-06
- 语料：`data/knowledge/injection.jsonl`（10）、`request-and-files.jsonl`（8）、`auth-deserialization.jsonl`（7），共 25 条
- 约束：本报告**不改动任何 jsonl 文件**，仅给出建议回填值，供用户审核后由主代理执行
- 核验方式：逐条读取 `technique` / `title` / `summary` / `payload_template` / `provenance.source_ref`，再联网检索权威来源及其发布时间

> ⚠️ 关于路径：任务描述里的路径是 `backend/data/knowledge/*.jsonl`，实际文件位于
> `C7F-W3B-Q1LL3R/data/knowledge/*.jsonl`（`backend/data/` 下只有会话/工作区数据）。本报告按实际路径处理。

---

## 1. 结论摘要

### 1.1 可信度分布

| 可信度 | 条目数 | 说明 |
|---|---|---|
| `confirmed` | 24 | 找到明确给出发布日期的权威页面。其中 19 条是 **WSTG v4.2 的版本级日期**，另有 5 条有独立明确日期 |
| `inferred` | 1 | `sqli-union-based`（PortSwigger Web Security Academy 主题无单页发布日期，用平台上线时间推断） |
| `unconfirmed` | 0 | 无「查不到任何可靠来源」的条目 |

### 1.2 最重要的发现：`2020-12-03` **不是**占位日期

`2020-12-03` 恰好是 **OWASP WSTG v4.2 的正式发布日**（官方多源一致）：

- https://wstg.owasp.org/downloads → `Version 4.2  December 3, 2020`
- https://owasp.org/projects/web-security-testing-guide?tab=release-versions → `[Version 4.2] - 2020-12-03`
- 项目负责人 Elie Saad 的发布公告邮件（2020-12-03）→ https://groups.google.com/a/owasp.org/g/cairo-chapter/c/-lf_u6rFiVs

也就是说，**20 条共用该日期的条目中，有 19 条引用的是 WSTG v4.2，这个日期本身是真实、可核验的版本级日期**（只是"技术首次公开"远早于此，见 §1.4 的注意点）。真正需要修改的只有：

| 需要修改的原因 | 条目 |
|---|---|
| 引用的是**持续更新的 Cheat Sheet**（无单一日期），用了 WSTG 的日期 | `java-deserialization-gadget` |
| 引用 PortSwigger WSA（无单页日期），当前 `2024-02-29` 与真实上线时间不符 | `sqli-union-based`、`ssti-jinja2-detection`、`jwt-key-confusion` |
| 引用的日期只是"月份近似"或"缺日" | `ssrf-cloud-metadata`（缺日）、`jwt-alg-none`（RFC 只有月份） |
| **引用来源本身不准确**（下节） | `client-template-injection` |

### 1.3 一处引用错误：`client-template-injection`

该条 `source_ref` 写的是 `OWASP WSTG v4.2: Testing for Client-side Template Injection`，
但经核对 WSTG 官方测试清单，**v4.2 中并不存在"Client-side Template Injection"这一测试项**：

- WSTG v4.1 / v4.2 的 Client-side 类别只到 `WSTG-CLNT-13`（Cross Site Script Inclusion），无 CLNT-14/15
  - 证据：https://github.com/tamnd/wstg-index （v4.1 清单）、Sayfer 审计报告中的 v4.2 清单 https://sayfer.io?p=1855/
- 该测试项 `WSTG-CLNT-15 Testing for Client-side Template Injection` **只存在于 WSTG v5.0 开发版**
  - 证据：https://owasp.github.io/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/11-Client-side/15-Client-Side_Template_Injection

因此我建议把该条改锚到该技术的**原始权威出处**：PortSwigger（Gareth Heyes）2016-01-27 的博文（见 §3）。这也是全语料中唯一一处 `source_ref` 与真实存在性不符的问题。

### 1.4 时间留出实验是否已可用：**部分可用，但还不能直接开跑**

分两步看：

1. **单条日期可核验性**：回填后 25 条全部有可核验的真实日期（24 confirmed + 1 inferred），不再有 `null`、也不再有"编造日期"。这一目标**可以达成**。

2. **`audit_published_at()` 的口径问题（关键）**：项目的审计工具 `backend/app/knowledge/audit.py:24` 把
   **"同一日期出现 ≥ 3 条"一律判为占位**（`DEFAULT_PLACEHOLDER_GROUP = 3`）。按我下面的建议回填后，仍有
   **19 条真实地落在 `2020-12-03`**（因为它们的引用来源就是同一份 WSTG v4.2），所以
   `time_split_usable` 仍会是 `False`。
   这不是数据造假问题，而是"工具启发式"与"一批知识确实同源同日发布"之间的冲突。

   全部条目回填后的日期上限是 `2020-12-03`，因此：
   - 若挑战 `start_at - buffer > 2020-12-03`：25 条全落在"可见"侧，**留出集为空，实验无区分度**；
   - 若挑战 `start_at` 落在 2008~2020 区间：19 条 WSTG 条目可用作留出集，实验才有效。

   **建议主代理二选一（都不涉及伪造）：**
   - **(A) 更细粒度引用**：为 19 条 WSTG 条目区分"该测试项首次进入 WSTG 的版本"，例如长青测试（SQLi/XSS/LDAP/命令注入/路径穿越/CSRF/Session Fixation/弱认证/文件上传/开放重定向/CORS/IDOR/XXE）首次出现在 **v3.0（2008-12-16）或 v4.0（2014-09-17）**，而
     `WSTG-INPV-19`（SSRF，`ssrf-internal-service`）是 **v4.2 新增**、`WSTG-INPV-18`（SSTI）与 NoSQL 子节也较晚
     （v4.1/v4.2 期间）。这需要主代理逐项核对 GitHub 版本历史后再回填（本报告未逐项验证每个测试的首版，故不在此给出具体年份以免失真）。
   - **(B) 放宽启发式**：对该语料把 `DEFAULT_PLACEHOLDER_GROUP` 调大，或在审计中把"同源批次日期"列入白名单。

   两者都需要用户拍板，我在此**不建议**用"给同一来源的条目人为编造不同日期"的方式满足工具。

---

## 2. 总表

| id | 文件 | 当前 `published_at` | 建议 `published_at` | 可信度 | 依据 URL |
|---|---|---|---|---|---|
| sqli-error-based | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| sqli-boolean-blind | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| sqli-union-based | injection | 2024-02-29 | **2019-04-02** | inferred | https://www.portswigger.net/blog/a-one-million-milestone-for-the-web-security-academy |
| sqli-time-based | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| command-injection-parameter | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| ssti-jinja2-detection | injection | 2024-02-29 | **2015-08-05** | confirmed | https://blackhat.com/docs/us-15/materials/us-15-Kettle-Server-Side-Template-Injection-RCE-For-The-Modern-Web-App-wp.pdf |
| client-template-injection | injection | 2020-12-03 | **2016-01-27** | confirmed（需改锚） | https://portswigger.net/blog/xss-without-html-client-side-template-injection-with-angularjs |
| xxe-file-read | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| nosql-operator-injection | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| ldap-injection-search-filter | injection | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| xss-reflected-context | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| xss-stored-persistence | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| ssrf-internal-service | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| ssrf-cloud-metadata | request-and-files | 2011-09-01 | **2011-09-28** | confirmed | https://nist.gov/publications/nist-definition-cloud-computing |
| path-traversal-file-read | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| unrestricted-file-upload | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| open-redirect-parameter | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| cors-origin-reflection | request-and-files | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| java-deserialization-gadget | auth-deserialization | 2020-12-03 | **2006-07-19**（或 `null`，见 §3） | confirmed（CWE 元数据） | https://cwe.mitre.org/data/definitions/502.html |
| session-fixation-prepost | auth-deserialization | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| default-credentials-auth-bypass | auth-deserialization | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| jwt-alg-none | auth-deserialization | 2015-05-01 | **2015-05-01**（不变，月份级） | confirmed（月份级） | https://www.rfc-editor.org/info/rfc7519 |
| jwt-key-confusion | auth-deserialization | 2024-02-29 | **2015-03-31** | confirmed | https://auth0.com/blog/2015/03/31/critical-vulnerabilities-in-json-web-token-libraries/ |
| idor-object-reference | auth-deserialization | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |
| csrf-state-changing-request | auth-deserialization | 2020-12-03 | **2020-12-03**（不变） | confirmed（版本级） | https://wstg.owasp.org/downloads |

---

## 3. 逐条说明

### injection.jsonl

**1. `sqli-error-based` — Error-based SQL injection**
引用 `OWASP WSTG v4.2: Testing for SQL Injection; CWE-89`。对应测试项 `WSTG-INPV-05`。
建议保留 `2020-12-03`（WSTG v4.2 版本级日期，confirmed）。
建议：`source_type = standard`，`source_ref = "OWASP WSTG v4.2 §4.7.5 (WSTG-INPV-05) Testing for SQL Injection; CWE-89"`，
`source_url = "https://owasp.org/www-project-web-security-testing-guide/v42/4-Web_Application_Security_Testing/07-Input_Validation_Testing/05-Testing_for_SQL_Injection"`。

**2. `sqli-boolean-blind` — Boolean-based blind SQL injection**
同样引用 WSTG v4.2 的 Blind SQLi，归属同一测试项 `WSTG-INPV-05`（v4.2 中 SQL 注入测试含盲注子节）。
保留 `2020-12-03`（confirmed，版本级）。`source_url` 同上。

**3. `sqli-union-based` — UNION-based SQL injection**
引用 `PortSwigger Web Security Academy: SQL injection UNION attacks`。WSA 是**持续更新**的在线教材，单页没有发布日期。
WSA 于 **2019 年 4 月**上线（PortSwigger 自述："We launched the Web Security Academy in April 2019"），
首批四个经典主题即含 SQL injection，因此 UNION attacks 页随主题同期上线。媒体记载软发布为 **2019-04-02**。
建议 `published_at = 2019-04-02`，标注为 **inferred**（平台/主题上线日，非该页快照日）。
建议 `source_type = vendor_doc`，`source_url = "https://portswigger.net/web-security/sql-injection/union-attacks"`。

**4. `sqli-time-based` — Time-based blind SQL injection**
同上，WSTG v4.2 `WSTG-INPV-05`（盲注/时间盲注）。保留 `2020-12-03`（confirmed，版本级）。

**5. `command-injection-parameter` — OS command injection**
对应 `WSTG-INPV-12 Testing for Command Injection`。保留 `2020-12-03`（confirmed，版本级）。
建议 `source_ref = "OWASP WSTG v4.2 (WSTG-INPV-12) Testing for Command Injection; CWE-78"`。

**6. `ssti-jinja2-detection` — SSTI in Jinja2**
引用 `PortSwigger WSA: Server-side template injection`。该技术的**原始权威出处**是 James Kettle 的白皮书
《Server-Side Template Injection: RCE for the modern webapp》，在 **Black Hat USA 2015（2015-08-05）** 首次公开（The Register 报道 2015-08-05）。
WSA 的 SSTI 主题上线约 **2020 年 4 月**（Hall of Fame 首次解题 2020-04-15）。
建议采用**已确认的原始论文日期 `2015-08-05`**（`confirmed`）；若坚持引用 WSA，则只能给 `2020-04-15`（inferred）。
建议 `source_type = research_paper`，`source_url = "https://portswigger.net/research/server-side-template-injection"`（并附 Black Hat 白皮书链接）。

**7. `client-template-injection` — Client-side template injection** ⚠️ 引用需修正
引用 `OWASP WSTG v4.2: Testing for Client-side Template Injection`——**该测试项不存在于 v4.2**（见 §1.3）。
原始权威出处应为 PortSwigger（Gareth Heyes）博文《XSS without HTML: Client-Side Template Injection with AngularJS》，
页面明确标注 **Published: Wednesday, 27 January 2016**。
建议 `published_at = 2016-01-27`（confirmed），`source_type = vendor_research`，
`source_ref = "PortSwigger (G. Heyes), XSS without HTML: Client-Side Template Injection with AngularJS"`，
`source_url = "https://portswigger.net/blog/xss-without-html-client-side-template-injection-with-angularjs"`；
并把 `CWE-79` 保留。

**8. `xxe-file-read` — XXE**
引用 `WSTG v4.2: Testing for XML Injection; CWE-611`。对应 `WSTG-INPV-07 Testing for XML Injection`（含 XXE）。
保留 `2020-12-03`（confirmed，版本级）。

**9. `nosql-operator-injection` — NoSQL operator injection**
引用 `WSTG v4.2: Testing for NoSQL Injection`。v4.2 中 NoSQL 注入是 SQL 注入测试（`WSTG-INPV-05`）的子节
（可标注为 `WSTG-INPV-05.6`）。保留 `2020-12-03`（confirmed，版本级）。
建议 `source_ref` 明确写成 `OWASP WSTG v4.2 (WSTG-INPV-05) NoSQL Injection; CWE-943` 以免被误读为独立测试项。

**10. `ldap-injection-search-filter` — LDAP filter injection**
对应 `WSTG-INPV-06 Testing for LDAP Injection`。保留 `2020-12-03`（confirmed，版本级）。

### request-and-files.jsonl

**11. `xss-reflected-context` — Reflected XSS**
对应 `WSTG-INPV-01 Testing for Reflected Cross Site Scripting`。保留 `2020-12-03`（confirmed，版本级）。

**12. `xss-stored-persistence` — Stored XSS**
对应 `WSTG-INPV-02 Testing for Stored Cross Site Scripting`。保留 `2020-12-03`（confirmed，版本级）。

**13. `ssrf-internal-service` — SSRF**
对应 `WSTG-INPV-19 Testing for Server-Side Request Forgery`。值得注意的是 **`WSTG-INPV-19` 是 v4.2 新增**（v4.1 清单止于 INPV-18），
所以 `2020-12-03` 对本条而言正好是"首次收录日"，语义最干净。保留（confirmed）。

**14. `ssrf-cloud-metadata` — Cloud metadata SSRF**
引用 `MITRE CWE-918; NIST SP 800-145`。NIST SP 800-145《The NIST Definition of Cloud Computing》
官方发布日 **2011-09-28**（csrc/nist.gov 均标注 September 28, 2011），当前 `2011-09-01` 缺日，建议改为 **2011-09-28**（confirmed）。
另可附 `CWE-918` 收录日 **2013-02-21** 作为更贴题的备选锚点（https://cwe.mitre.org/data/definitions/918.html）。
建议 `source_type = standard`，`source_url = "https://csrc.nist.gov/publications/detail/sp/800-145/final"`。

**15. `path-traversal-file-read` — Path traversal**
引用 `WSTG v4.2: Testing for Path Traversal`。v4.2 中对应 `WSTG-ATHZ-01 Testing Directory Traversal File Include`
（名称与引用略有差异，但确为同一测试）。保留 `2020-12-03`（confirmed，版本级）。

**16. `unrestricted-file-upload` — Unrestricted file upload**
引用 `WSTG v4.2: Testing for Unrestricted File Upload`。v4.2 拆分为 `WSTG-BUSL-08 Test Upload of Unexpected File Types`
与 `WSTG-BUSL-09 Test Upload of Malicious Files`。保留 `2020-12-03`（confirmed，版本级，建议补注 BUSL-08/09）。

**17. `open-redirect-parameter` — Open redirect**
引用 `WSTG v4.2: Testing for Open Redirections`。v4.2 中对应 `WSTG-CLNT-04 Testing for Client-side URL Redirect`
（"Open Redirections"为 v4.0/更早的名称）。保留 `2020-12-03`（confirmed，版本级）。

**18. `cors-origin-reflection` — CORS origin reflection**
对应 `WSTG-CLNT-07 Testing Cross Origin Resource Sharing`。保留 `2020-12-03`（confirmed，版本级）。

### auth-deserialization.jsonl

**19. `java-deserialization-gadget` — Insecure deserialization** ⚠️ 唯一需要"改锚或置 null"的条目
引用 `OWASP Deserialization Cheat Sheet; CWE-502`。**Cheat Sheet 是持续更新的页面，没有单一发布日期**
（OWASP 官方也明说 Cheat Sheet Series 是不断迭代的；能查到的最早 wiki 修订为 2016-06-29，2019-02-14 迁移到 GitHub）。
因此有两条都不造假的路径：
- **(a) 改锚到 `CWE-502`**，其收录日为 **2006-07-19**（CWE Draft 3），建议 `published_at = 2006-07-19`（confirmed，但请注意这是**弱点条目元数据日期**，不是"Java gadget 链被公开"的日期）；
- **(b) 改锚到 `OWASP Top 10 2017 A8:2017-Insecure Deserialization`**，正式发布 **2017-11-20**（confirme​d），语义上更贴近该条内容（https://owasp.org/www-project-top-ten/2017/Top_10 ）；
- **(c) 若必须保留 Cheat Sheet 作为 `source_ref`**，则诚实做法是 `published_at = null`。
同一 CVE 层的强相关日期还有 `CVE-2015-4852`（Apache Commons Collections，2015），如需"gadget 时代"锚点可另引。

**20. `session-fixation-prepost` — Session fixation**
对应 `WSTG-SESS-03 Testing for Session Fixation`。保留 `2020-12-03`（confirmed，版本级）。

**21. `default-credentials-auth-bypass` — Default credentials / weak auth**
引用 `WSTG v4.2: Testing for Weak Authentication`。v4.2 的 Authentication 类别含 `WSTG-ATHN-02 Testing for Default Credentials`
（以及 ATHN-07 弱口令策略）。保留 `2020-12-03`（confirmed，版本级，建议补注 ATHN-02）。

**22. `jwt-alg-none` — JWT alg none**
引用 `RFC 7519: JWT; CWE-347`。RFC 7519 官方只给**月份**"May 2015"（IETF 公告邮件 2015-05-19），
故 `2015-05-01` 属**月份级 confirmed**，可保留（ISO date 需补日，取 01 为惯例）。
更贴题的补充来源：Tim McLean《jwt-alg-none》**2015-02-25**（https://www.timmclean.net/2015/02/25/jwt-alg-none.html ）；
另注：`alg:none` 的定义实际在 **RFC 7518 §3.6**，而"必须服务端固定算法"的最佳实践在 **RFC 8725（2020-02）**。
若追求"漏洞首次公开"语义，建议改锚到 McLean 2015-02-25；若保持 RFC 引用，则 `2015-05-01` 即可。

**23. `jwt-key-confusion` — JWT algorithm confusion**
引用 `PortSwigger WSA: JWT attacks`。WSA 的 JWT 主题上线约 **2022-06-13**（Hall of Fame 首次解题 2022-06-13；Burp 2022.5.1 起支持 JWT 扫描）。
该技术的**原始披露**是 Tim McLean 的算法混淆问题，由 Auth0 于 **2015-03-31** 转发并公告（blog 2015/03/31）。
建议采用**已确认的 `2015-03-31`**（改锚到原始披露，confirmed）；
若保留 WSA 引用，则只能给 `2022-06-13`（inferred）。
建议 `source_type = vendor_research` / `advisory`，
`source_url = "https://auth0.com/blog/2015/03/31/critical-vulnerabilities-in-json-web-token-libraries/"`。

**24. `idor-object-reference` — IDOR**
对应 `WSTG-ATHZ-04 Testing for Insecure Direct Object References`。保留 `2020-12-03`（confirmed，版本级）。

**25. `csrf-state-changing-request` — CSRF**
对应 `WSTG-SESS-05 Testing for Cross Site Request Forgery`。保留 `2020-12-03`（confirmed，版本级）。

---

## 4. 需要主代理重点复核的地方

1. **19 条 WSTG 条目仍共享 `2020-12-03`，会继续触发 `audit_published_at()` 的"≥3 同日期=占位"判定**
   （`backend/app/knowledge/audit.py:24`）。这是本任务的根本张力：日期是真实的，但工具口径会拒绝。
   **需要用户决定 §1.4 的方案 (A) 逐项细化版本，还是 (B) 放宽启发式。切勿为过审计而编造日期。**

2. **`client-template-injection` 的 `source_ref` 引用了不存在的 WSTG v4.2 测试项**（实为 v5 的 `WSTG-CLNT-15`）。已建议改锚到 2016-01-27 的 PortSwigger 原文。

3. **三条 PortSwigger 条目的日期语义**：`2019-04-02`（inferred）、`2015-08-05` / `2015-03-31`（confirmed）都是"来源首次公开"而非"该页快照"。WSA 页面会持续更新，回填时建议在 `provenance.notes` 里写明"日期为来源首次公开发布日"。

4. **`java-deserialization-gadget` 需要用户拍板**：`2006-07-19`（CWE-502 元数据）/ `2017-11-20`（Top 10 2017 A8）/ `null`（保留 Cheat Sheet）三选一。语义差异较大，不建议由我单方面决定。

5. **`jwt-alg-none` 的 RFC 小瑕疵**：`alg:none` 由 RFC 7518 §3.6 定义，而非 RFC 7519。若要更严谨可补 `RFC 7518`。

6. **若干 `source_ref` 用的是旧版名称**（Path Traversal→ATHZ-01、Unrestricted File Upload→BUSL-08/09、Open Redirections→CLNT-04、Weak Authentication→ATHN-02）。这些不影响日期，但建议顺手把 v4.2 的测试编号补上，便于引用可追溯。

---

## 5. 引用的权威来源 URL 汇总

| 来源 | URL | 关键日期 |
|---|---|---|
| OWASP WSTG 历史版本 | https://wstg.owasp.org/downloads | v4.2 = 2020-12-03；v4.1 = 2020-04-21；v4.0 = 2014-09-17；v3.0 = 2008-12-16 |
| OWASP WSTG 项目页 | https://owasp.org/projects/web-security-testing-guide?tab=release-versions | v4.2 = 2020-12-03 |
| WSTG v4.2 发布邮件 | https://groups.google.com/a/owasp.org/g/cairo-chapter/c/-lf_u6rFiVs | 2020-12-03 |
| WSTG 测试项清单（v4.1/v4.2） | https://github.com/tamnd/wstg-index ；https://sayfer.io?p=1855/ | 用于确认测试编号存在性 |
| WSTG-CLNT-15（仅 v5/dev） | https://owasp.github.io/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/11-Client-side/15-Client-Side_Template_Injection | 说明 CSTI 不在 v4.2 |
| PortSwigger WSA 上线 | https://www.portswigger.net/blog/a-one-million-milestone-for-the-web-security-academy | 2019-04 |
| PortSwigger WSA SSTI 主题 | https://portswigger.net/web-security/hall-of-fame/server-side-template-injection | 首次解题 2020-04-15 |
| PortSwigger WSA JWT 主题 | https://portswigger.net/web-security/hall-of-fame/jwt | 首次解题 2022-06-13 |
| Kettle SSTI 白皮书（BH USA 2015） | https://blackhat.com/docs/us-15/materials/us-15-Kettle-Server-Side-Template-Injection-RCE-For-The-Modern-Web-App-wp.pdf | 2015-08-05 |
| Heyes CSTI（AngularJS） | https://portswigger.net/blog/xss-without-html-client-side-template-injection-with-angularjs | 2016-01-27 |
| NIST SP 800-145 | https://csrc.nist.gov/publications/detail/sp/800-145/final | 2011-09-28 |
| CWE-918 | https://cwe.mitre.org/data/definitions/918.html | 2013-02-21 |
| CWE-502 | https://cwe.mitre.org/data/definitions/502.html | 2006-07-19 |
| OWASP Top 10 2017 | https://owasp.org/www-project-top-ten/2017/Top_10 | 2017-11-20 |
| OWASP Deserialization Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html | 页面级，无单一日期 |
| RFC 7519 | https://www.rfc-editor.org/info/rfc7519 | May 2015（月份级） |
| Tim McLean jwt-alg-none | https://www.timmclean.net/2015/02/25/jwt-alg-none.html | 2015-02-25 |
| Auth0 算法混淆披露 | https://auth0.com/blog/2015/03/31/critical-vulnerabilities-in-json-web-token-libraries/ | 2015-03-31 |
