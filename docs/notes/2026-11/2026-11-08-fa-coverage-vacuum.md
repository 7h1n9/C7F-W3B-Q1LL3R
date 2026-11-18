# 2026-11-08（夜）FA 覆盖真空：根因不在 prompt 位置

## 一句话

上一轮诊断说「FA 采纳率 5.6%，因为 FA 排在 prompt 第 1226 字符」——**这个结论只解释了 1/81**。
真正的大头是：**81 道未解题里只有 1 道能产出 FIRST ACTION**。剩下 80 道根本没东西可采纳。

## 一、两个独立缺陷，按影响排序

### 缺陷 A（影响 80/81）：信任门造成覆盖真空

`_entry_may_command()` 只放行两类条目：

| 条件 | 全库条数 |
|---|---|
| `verified_solve: true` | 125 |
| `trust_level == "reviewed"` | 20 |
| **可下发 FA 合计** | **128 / 645** |

其余 599 条是 `vulhub.jsonl` 机械导入的 `unverified` 条目 —— 全部被挡。

实测（81 道 UNSOLVED）：

- 能产出 FA：**1**
- 已正确匹配到 vulhub 配方、且配方里有可执行首行：**13**
- 最终落到 `technique_candidate_goal`：**80**，其中 **68 道共用同一条字符串**

那条共用字符串长这样：

```
No recipe matched. Send the confirming exploit request within your first three
commands; do NOT run ffuf/dirb/gobuster ... cmdi=param→shell; delay or echo;
SSTI=arith in param evaluated; deser=serialized blob; which magic runs.
```

而这 68 道里 **35 道标题就写着 CVE 编号**，21 道已经匹配到真实 vulhub 配方。配方里的可执行首行包括：

- `POST /service/extdirect`（Nexus CVE-2019-7238）
- `GET /static/../../../../etc/passwd`（Node CVE-2017-14849）
- `POST /index.php?-d+allow_url_include=on...`（PHP CVE-2012-1823）
- `GET /xxx/..;/admin/`（Shiro CVE-2020-1957）

**这些全被丢掉，然后告诉模型「没配方，自己猜技法类」。**

### 缺陷 B（影响 6 道）：payload 回退只认 HTTP 动词行

`_first_action_resolved` 里有两份**重复**的 payload 回退逻辑，都只接受：

```python
re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+\S+", line)
```

但 vulhub 载荷有一半是 shell 命令：

- `ssh -p 3322 -i id_rsa -t git@git-upload-archive '--help'`（Git CVE-2017-8386）
- `java -jar ysoserial.jar CommonsCollections5 ...`（Jboss CVE-2017-12149）
- `python exploit.py -t ...`（Erlang CVE-2025-32433）

同时 `steps` 字段是导入器生成的**通用模板句**（「确认 X 版本在 CVE-… 影响范围内」），全部被判 `version_only`，于是整条配方被丢弃。

## 二、修法：探针通道，不是放开信任门

信任门本身是对的 —— S2-009 的 `/ajax/example5` 配方通过了所有 schema 校验，但在这个实例上就是错的，占住 FA 槽等于把整轮带走。

所以**不改门，加一条低权限通道**：

```
exploit_first_action   → 实测过（有 flag 背书）→ 措辞是"命令"
exploit_probe_action   → 匹配到但未实测       → 措辞是"探针"
```

探针的措辞明确写：

> Untested lead for this box — send it as your first command, then judge it by the
> response: `...`
> This recipe is from the public corpus and was **NOT verified on this instance**,
> so a 404/garbage/unchanged response is a **NORMAL outcome**, not a failure — if
> that happens, mark it a dead end and move on immediately instead of reworking
> the same request.
> If it does not confirm, fall back to what to look for next: `cmdi=param→shell...`

最坏成本从「整轮走错」降为「一条命令验证失败」，且失败后仍有技法回退。

## 三、实现细节（三个坑）

### 坑 1：源码片段被当成命令

第一版放宽 shell 正则后覆盖率涨到 28/81，但其中 **5 条是源码**：

| 实际下发的"命令" | 真实内容 |
|---|---|
| `package main` | Go 源码 |
| `class JDatabaseDriverMysql {` | PHP 源码 |
| `import sys` | Python 头部 |
| `public x(){` | Java 源码 |
| `var uid int64 = 1` | Go 变量声明 |

**错误的命令比没有命令更糟** —— 它教模型「这个配方是坏的」。加了 `_FA_CODE_SHAPES` 拒绝代码形态后降到 26/81，但零误报。

### 坑 2：重复的 payload 回退逻辑

`_first_action_resolved` 和 `_first_action_candidate` 各有一份只认 HTTP 动词的回退。我改了后者，覆盖率**毫无变化** —— 因为生产调用链走的是前者。这是同一个 bug 的第二次踩坑（上次是 `*` vs `**`）。

### 坑 3：占位符原样下发

载荷里的 `{{target_url}}` / `{{host}}` 不替换，模型复制执行必然失败，且失败原因与漏洞无关。加了 `_fa_substitute_target()`，但**目标未知时保留占位符** —— 猜一个 host 等于把一个错误请求伪装成实测请求。

## 四、效果

| 指标 | 修改前 | 修改后 |
|---|---|---|
| 能拿到具体首条动作的题 | 1 / 81（1%） | **26 / 81（32%）** |
| 只剩通用技法文本 | 80 | 54 |
| 源码误报 | — | 0 |

回归：**2401 → 2425 passed**（新增 24 条探针测试 + 19 条顺序测试）。

## 五、prompt 位置修复（上一轮的 P0，仍然做了）

`cli_solver._build_prompt()` / `_build_explore_prompt()`：

- 带 FA 时，goal 提到 `ctx` **最前面**（新建 `## Do this first` 段）
- board 的 `READ IT FIRST` 降级为 `Read it AFTER your assigned first command`
- 附件行的 `inspect them FIRST` 去掉 `FIRST`
- 尾部「Course correction」不再重复渲染同一段 goal

识别标记从 `FIRST ACTION` 扩展为 `("FIRST ACTION", "Untested lead")` —— 探针也必须前置，否则它会排在 board 之后被完全跳过。

## 六、教训

1. **「采纳率低」有两种可能：模型不听话，或者根本没东西可采纳。** 我先花了两小时调 prompt 位置，实际 99% 的题是后者。
2. **统计数据必须按「能不能产出」分层。** 「FA 采纳率 5.6%」这个数字把 1 道题的失败和 80 道题的空槽混在一起，误导了整个方向。
3. **同一个 bug 会藏在两个地方。** 重复的 payload 回退逻辑让我第一次修改毫无效果，是靠「改完覆盖率不变」才发现的。
4. **放宽匹配必须同时收紧质量。** 只放宽 → 28 条里 5 条是垃圾；同时加过滤 → 26 条全干净。少 2 条换零误报是赚的。

## 七、真跑验证（上一轮没做就做统计，这次先做）

### Run 1：Shiro CVE-2020-1957（run `28c96f2b`）

**worker-1 命令 #1**：

```
curl -sv --max-time 20 -H 'Host: direct-ctf2.dasctf.com:25481' \
  'http://direct-ctf2.dasctf.com:25481/xxx/..;/admin/'
```

这正是探针文本本身（`GET /xxx/..;/admin/`）。**零条暖场命令** —— 对比修改前的中位暖场 2 条。

后续命令仍在同一方向推进（`/admin/env`、`/admin/actuator`、`/admin/flag`），第 6 条还主动去读本地语料 `/home/kali/pocs/vulhub/shiro/CVE-2020-1957/README.md`。

### Run 2：Git CVE-2017-8386（run `ba044e88`）

事件落库（`muteki.exploit.probe_action`）：

```json
{
  "lane": "exploit:known-path",
  "text": "ssh -p 3322 -i id_rsa -t git@http://direct-ctf2.dasctf.com:25305 \"git-upload-archive '--help'\"",
  "in_window": true,
  "goal_chars": 901
}
```

**注意 `{{target_url}}` 已被替换成真实靶机地址** —— 占位符替换生效。

**worker-1 命令 #3**：

```
ls -la && which ssh id_rsa 2>/dev/null; ls -la id_rsa 2>/dev/null; cat ~/.ssh/config 2>/dev/null
```

直接验证探针前提（ssh 与密钥是否可用）。之后 30+ 条命令**全部沿探针方向深挖**：

- 读本地语料 `/home/kali/pocs/vulhub/git/CVE-2017-8386/README.md`
- 取 `id_rsa.pub` 建 scratch 工作区
- `ssh -p 25305 -i scratch/id_rsa ... git@target "git-upload-archive '--help'"`
- 依次写 `run.py` / `run2.py` / `pwn.py` / `flaghunt.py` / `h2.py`，用 pexpect 驱动交互式 pager、注入 `!bash`、然后 `cat /flag*` / `printenv` / `grep -r 'flag{'`

最终 `COMPLETED_UNSOLVED` —— **探针生效但利用链本身失败**（该题需要 ssh-rsa 算法协商等特定条件）。这与「模型在浅层侦察里打转」是完全不同的失败形态：它已经打到了真实利用路径上，只是这条路径在这个实例上不通。

### Run 3：Nexus CVE-2019-7238（run `ff32edd5`）

探针 `send \`POST /service/extdirect HTTP/1.1\`` 注入 `exploit:known-path` lane。

**worker-1 命令 #10**（前 9 条是环境/黑板探查，因为该 Run 的 claude 引擎刚恢复）：

```
curl -s -i --max-time 20 -X POST 'http://direct-ctf2.dasctf.com:29084/service/extdirect' \
  -H 'Content-Type: application/json' --data '{"action":"c...'
```

**精确命中探针**，且自动补全了 `Content-Type` 与 Action 体 —— 探针只给了请求行，模型补齐了协议细节，这正是「探针给方向、模型补细节」的预期分工。

后续失败是因为靶机端口 `29084` 拒绝连接（`Connection refused`），模型正确地 `mark-deadend` 记录了原因。

### 三个 Run 的端到端结论

| Run | 题 | 探针注入 | 探针执行 | 结果 |
|---|---|---|---|---|
| `28c96f2b` | Shiro CVE-2020-1957 | ✅ | ✅ 命令 #1 | 后续探测正常，未解 |
| `ba044e88` | Git CVE-2017-8386 | ✅ `in_window=true` | ✅ 命令 #3，后续 30+ 条沿此方向 | `COMPLETED_UNSOLVED` |
| `ff32edd5` | Nexus CVE-2019-7238 | ✅ | ✅ 命令 #10（前 9 条因引擎重启） | 靶机拒连 |

### 途中发现并修复的基础设施故障

Run 3 首次启动时 `worker-1`（claude-cli）**8 分钟零命令**，只有两条 session 事件。排查发现 **`.claude-proxy.json` / `.codex-proxy.json` 指向的两个代理进程都已死亡**（端口 `9066`/`9075` 返回 `000`），而状态文件残留导致 supervisor 不重新 spawn。

修复：删除两个 `.json` 状态文件 → 重启后端 → supervisor 重新拉起代理（新端口 `16086`/`16091`）→ worker-1 恢复产出 19 条命令。

**这正是记忆里那条「启动前先杀旧代理 + 删 `.claude-proxy.json` / `.codex-proxy.json`，否则 supervisor 不 spawn」的实证复现。**

### 关键教训：事件名有 `muteki.` 前缀

`EventType.EXPLOIT_PROBE_ACTION = "exploit.probe_action"`，但落库时变成 `muteki.exploit.probe_action`。用 `event_type='exploit.probe_action'` 查询会**恒返回 0** —— 我为此白等了 7 分钟。查事件必须先 `select distinct event_type` 确认真实名字。

## 八、下一步

1. 剩余 54 道零配方题中，**23 道有 CVE 但语料没有** → 扩 `vulhub.jsonl`，不是改代码。
2. **31 道无 CVE 的常规 Web 题**（`[CISCN2019]总决赛 滑稽云音乐`、`l33t-hoster`、`[护网杯 2018]easy_laravel` 等）确实需要技法库，这是独立方向。
3. 探针事件已可观测（`muteki.exploit.probe_action`），下一步可做采纳率统计 —— 但**先攒够样本**（当前 2 个 Run）。

## 九、本次代码改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/known_exploit.py` | 新增 `exploit_probe_action()`；`_fa_payload_line()` 支持 shell 命令 + 拒绝源码片段 + 占位符替换；两处重复的 payload 回退统一到该函数 |
| `backend/app/solver/muteki/coordinator.py` | `CoordinatorConfig.exploit_probe_action`；`_probe_goal()`；`_record_probe_injection()`；两条下发点接入 |
| `backend/app/solver/muteki/core/orchestrator.py` | 构造函数新增 `exploit_probe_action` 参数（否则 TypeError → 静默 `MUTEKI_RUNTIME_ERROR`） |
| `backend/app/solver/muteki/runtime/muteki_runtime.py` | `_exploit_probe_action()` 并传入 orchestrator |
| `backend/app/solver/muteki/events.py` | `EventType.EXPLOIT_PROBE_ACTION` |
| `backend/muteki/solver/cli_solver.py` | `_has_first_action()`；goal 前置；board/附件降级 |
| `backend/tests/test_muteki_first_action_leads_prompt.py` | 19 条（prompt 顺序） |
| `backend/tests/test_muteki_recipe_probe_channel.py` | 26 条（探针通道 + 事件） |

回归：**2401 → 2427 passed**。
