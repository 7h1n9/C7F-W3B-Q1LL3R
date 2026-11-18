# CTF Writeup（解题报告）

## 题目信息与结果

- **题目：**WriteSHell
- **目标：**http://3e5571b3500a0ca8c0312716.http-ctf2.dasctf.com/
- **解题模式：**muteki
- **解题状态：**已解出
- **漏洞类型：**任意文件写入（ARBITRARY_FILE_WRITE）（模型判定） / 通用 Web 漏洞（GENERIC_WEB）（启发式）
- **黑板判定：**待由已验证事实确认
- **复杂度：**中等（评分 27）
- **语义分析：**未运行（0 项）
- **LLM 章节：已生成（模型 kimi-k3；无可用 Worker 对话）**

## 一句话解法

题为 WriteSHell，目标疑似存在文件写入类漏洞；Run 状态为 COMPLETED_SOLVED，但无 Worker 对话与具体请求记录，无法从证据还原确切入口与参数。

## 攻击链

1. 侦察：访问目标根路径，收集页面、表单与接口信息
2. 发现：定位可写入文件（疑似写 shell）的入口或参数
3. 利用：构造请求向 Web 目录写入恶意脚本/读取 flag
4. 取 Flag：访问写入的文件或触发回显，匹配 DASCTF{...}

## 执行过程

**1. 启动三个 Native Worker 并行解题**

Run 调度 3 次 muteki_native_worker（claude-cli 的 worker-2，codex-cli 的 worker-1 与 worker-3），均返回 COMPLETED，产物仅记录「official CliSolver completed one bounded Intent」，无命令与输出原文。

**2. Run 结束并标记已解出**

run.status=COMPLETED_SOLVED，耗时 598s；但 board.facts、timeline、verified_flag 均为空，无法还原任何一次 HTTP 请求或观察到的响应。


## 已确认事实与证据链

- **[中等]** robots.txt at /robots.txt discloses /static/secretkey.txt; its real 20-byte body is 'you-will-never-guess' (not a DASCTF flag).

- **[中等]** Root / is PHP source: highlight_file(__FILE__) then builds md5(uniqid()).php via file_put_contents using GET cmd, and var_dump()s the generated filename on every request.

- **[中等]** Public .git is directory-indexed: HEAD=refs/heads/master, master=15ca375e54f056a576905b41a417b413c57df6eb, config origin=https://github.com/fermayo/hello-world-lamp.git. All 11 loose objects are upstream LICENSE/README/index.php/logo.png/phpinfo.php/tree/commit objects; .git/index is 5 entries match

- **[中等]** Framework surface leaks X-Powered-By: PHP/5.5.9-1ubuntu4.14; front proxy is openresty but Apache error pages identify Apache/2.4.7 (Ubuntu), including autoindex pages for /.git/.

- **[中等]** Bounded dirb top-1000 public-path scan of / and /static/ found no new readable files: root only reveals .git/HEAD (200), .hta/.htaccess/.htpasswd (403), cgi-bin/ (403); /static/ top-1000 had zero non-404 results. JSON evidence: /home/kali/workspace/evidence/ffuf-root-top1000.json and ffuf-static-top

- **[中等]** [codex] `robots.txt` discloses `/static/secretkey.txt`; live body is `you-will-never-guess` (not a flag).
  证据引用：26578337346d

- **[中等]** WriteSHell RCE confirmed: cmd=' . system('id') . ' writes a PHP file whose leaked md5(uniqid()).php body is uid=33(www-data) gid=33(www-data) groups=33(www-data); a root-level /flaaaaaaaaaaaaaaaaag file is readable via the same generated-shell execution path.


### 证据台账（Evidence Ledger）

- 0ce8d7fd-49fe-526e-b094-c6760e47af4b · Muteki 原生 Worker 输出 · 已验证 · Native Muteki Worker output retained as a protected artifact.
- a64e4181-b5d4-5f95-83ba-83661ca1fe91 · Muteki 原生 Worker 输出 · 已验证 · Native Muteki Worker output retained as a protected artifact.
- a87c3694-dbec-5123-a4e1-4eec552a4d1f · Muteki 原生 Worker 输出 · 已验证 · Native Muteki Worker output retained as a protected artifact.

## 可复现方式

> 以下执行命令/脚本由模型撰写，**尚未在本管线内自动执行验证**。

## 环境要求

- 可访问目标：http://3e5571b3500a0ca8c0312716.http-ctf2.dasctf.com/
- curl 与 Python 3

## 说明

由于本次 Run 未保留任何 Worker 请求/响应原文（worker_conversation.available=false），无法给出确定的可复现脚本。以下为「发现式」复现框架：先抓取站点原始响应，再依据实际返回内容判断写文件入口；所有路径与字段均为待验证假设，不得视为已确认事实。

## 第一步：抓取根路径原始响应

```bash
curl -i -s http://3e5571b3500a0ca8c0312716.http-ctf2.dasctf.com/
```

预期输出：完整 HTTP 头与 HTML 正文，据此寻找表单 action、上传/写入相关参数（如 filename、content 之类，具体字段需以实际响应为准）。

## 第二步：发现式探测（Python）

```python
import requests

BASE = "http://3e5571b3500a0ca8c0312716.http-ctf2.dasctf.com/"

r = requests.get(BASE, timeout=10)
print(r.status_code)
print(r.text)  # 先打印原始响应，人工确认写文件入口与字段名后再构造利用请求
```

## 预期输出 / flag 位置

按题目名 WriteSHell 推测，flag 应通过写入 shell 后执行命令（如 `cat /flag`）获得，格式为 `DASCTF{...}`；但该路径未在本次证据中出现，属未验证猜测。


## 失败路线与策略切换

- 死胡同：Public backup/config enumeration ruled out: /composer.json, /vendor/, /config.php, /db.sql, /uploads/, /backups/, /index.php.bak/~/.save/.swp all return 404.
- 死胡同：Basic path traversal attempts (/../, encoded traversal, .git traversal) return 404/400 via openresty; no public LFI through those paths.
- 死胡同：Exposed upstream Git history contains no challenge-specific source or flag; did not waste further recon on it.
- 死胡同：Public namespace enumeration is exhausted for this lane: no additional readable endpoint beyond /, /robots.txt, /static/secretkey.txt, and directory-indexed /.git/.
- 死胡同：[codex] Public backup/config enumeration returned no hits for `/composer.json`, `/vendor/`, `/config.php`, `/db.sql`, `/uploads/`, `/backups/`, or editor/backup variants.
- 死胡同：[codex] Basic encoded and unencoded path traversal did not expose local files.
- 死胡同：[codex] The exposed Git history contains no challenge-specific source or flag.
- 死胡同：[codex] No additional readable public endpoint was found beyond `/`, `/robots.txt`, `/static/secretkey.txt`, and directory-indexed `/.git/`.
- 模型补充：
  - 证据还原路线走不通：board 的 facts/timeline/dead_ends 全部为空，verified_flag 为空，无法交叉核对任何利用细节
  - Worker 对话原文不可用：三份 evidence 文件各仅 47 字符，内容仅为完成声明，不含命令与输出

## 完成门禁（Completion Gate）

- 结果判定 / Flag：已通过
- 是否有证据支撑：是
- 已验证 Flag：CTF2{1aa54529-0abd-45f1-85f8-29cafcbea2ad}

## 修复建议

- 对任何文件写入接口做严格校验：限制目标目录（固定白名单目录 + realpath 检查，阻断 ../ 路径穿越），禁止用户控制完整路径
- 对写入文件的扩展名与 MIME 做白名单，禁止写入 .php/.jsp 等可执行脚本类型；Web 目录配置为不可执行（如 PHP 中禁用写入目录的引擎解析）
- 写入内容做安全处理：若以键值/配置形式落盘，对值做转义或序列化校验，防止注入可执行代码
- 最小权限运行 Web 进程，敏感文件（如 /flag）仅 root 可读，并考虑用独立 flag 服务而非文件存放
- 对写文件接口加认证与频率限制，并记录审计日志

- 本次实际使用工具: muteki_native_worker


## 报告来源

- 叙事章节（一句话解法 / 攻击链 / 执行过程 / 可复现方式 / 修复建议）由 LLM 依据黑板事实与事件时间线（未捕获到可用的 Worker 对话，仅 278 字符占位记录）生成，输入 278 字符。
- 生成开销：模型 kimi-k3，tokens 2462。
- 题目信息 / 已确认事实与证据链 / 证据台账 / 完成门禁 / 工具清单均由代码按数据库与黑板状态生成，不做改写。
- 模型标注的证据缺口：证据严重不足：worker_conversation.available=false，三份 worker 产物仅含 47 字符的完成声明；board.facts/timeline/dead_ends/verified_flag 全部为空，没有任何一次真实 HTTP 请求、响应、命令输出或 flag 原文。one_liner/attack_chain/vuln_type 中关于「文件写入 shell」的判断仅依据题目名 WriteSHell 推断，属未验证猜测；repro 中的路径、参数名、flag 位置均无证据支撑。缺失内容：Worker 实际发出的请求方法与路径、请求参数、响应字段名、flag 具体值与获取方式。
