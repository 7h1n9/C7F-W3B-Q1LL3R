# 工具环境 + 外部研究：三个可落地方向

2026-11-06 15:40。用户提了两点：Kali 环境其实可以善用；上网看看别人怎么做的。

以下每条都先给数据，再给判断。**没有数据的想法不写进这里。**

---

## 一、工具环境：装得不少，用得很少，用得也浅

### 实际装的（`ghcr.io/fishcodetech/muteki-worker`）

| 有 | 没有 |
|---|---|
| sqlmap、ffuf、gobuster、dirb | nuclei |
| hydra、nmap、nikto、whatweb | tplmap、arjun |
| commix、jq、php、java | xxe 注入工具 |
| **nc、expect、tmux** | |

### 实际用的（484 个 Run 汇总）

| 工具 | 调用次数 |
|---|---|
| jq | 614（其实是在处理 JSON，不算安全工具）|
| ffuf | 304 |
| dirb | 216 |
| gobuster | 82 |
| nmap | 46 |
| **sqlmap** | **13** |
| nuclei | 13（容器里根本没装，这是误匹配或调用了外部）|
| nikto | 12 |
| whatweb | 6 |
| **hydra** | **2** |
| **commix** | **0** |

**SOLVED 平均只用 0.22 种工具，UNSOLVED 用 0.47 种。** 用工具多的反而没解出。

这条数据很重要：它说明**「给更多工具」不是主要杠杆**。Worker 手写 curl 反而更容易解出——因为手写时它知道每个字节在干什么。

### 但有一样东西是真的缺：实时交互

CTFAgent 论文（CCS'25）指出的核心问题，在我们的数据里完全成立：

| 交互形态 | 用过的 Run | 其中伴随超时/断连 |
|---|---|---|
| heredoc 喂输入 | 202 (41%) | **149** |
| timeout 包一层 | 107 (22%) | 87 |
| **nc 反弹/连接** | 71 (14%) | **55 (77%)** |
| 管道填 stdin | 10 (2%) | 7 |
| **expect 驱动** | **8 (1%)** | 6 |

`nc` 用了 71 轮，**77% 伴随超时或断连**。而 `expect` 只用了 8 轮——容器里装了，但几乎没人用。

实际形态：Worker 在自己造交互层——`printf 'GET / HTTP/1.1\r\n...' | nc`，甚至 `tmux new-session -d -s listener 'nc -lvnp 8888 > exfil.log'`。

**判断**：它已经意识到缺这个能力，在用 shell 硬拼。给它一个真正的「发一条、收一条」原语，比自己拼 tmux 便宜得多。

---

## 二、外部研究：四条可直接借用的

### 1. CTFAgent 的两阶段 RAG（CCS'25，Intercode-CTF 39→73/100）

- **RAG-Understanding**：用**漏洞代码片段**当检索键，把「可能的漏洞类型」提示回来
- **RAG-Exploiting**：用模型自己输出的**利用想法**当检索键，把「怎么利用」提示回来

关键洞察：**LLM 已经知道大部分技术知识，缺的是「把知识和具体场景对上」**。CTFKnow 实测：单选题很准，开放题断崖。

我们现在的做法是**一次性把整本手册塞进去**，和这个思路正好相反。

### 2. Strix 的 `load_skill`（34K+ stars）

- 51 份纯 Markdown 技能包，按漏洞类型/框架/协议/工具分类
- **agent 按需 `load_skill` 加载**，不是全塞
- 系统提示里明确禁止 "kitchen sink agent"（什么都干的 agent）

这直接印证了我上午的判断：我们的 `EXECUTION_CHECKLIST` 4733 字符塞了 7 条经验给每一道题，已经变成 kitchen sink。

### 3. Strix 的「非法 final output」机制

> 如果 agent 输出纯文本就结束，会被判为「非法 final output」，往 session 塞一条 recovery 消息强制继续，超过 max_turns 直接标 crashed。

这是用**工具契约**硬约束「LLM 偷懒提前停」——而不是用 prompt 请求它别停。

**对应我们的问题**：我上午加的 `DEADEND_DISCIPLINE` 是一段 prompt，请求它「写完 deadend 就停」。而 PHP 那轮的数据证明 **prompt 拦不住**——它写完 deadend 又跑了 10 条。

### 4. 一个反直觉的数据点（CSA 白皮书）

> 只给 CVE 描述，GPT-4 利用成功率 **87%**；**去掉 CVE 描述，降到 7%**。

这说明：**CVE 描述本身就是最大的信息杠杆**。我们给 Worker 的 `payload_template` 可能还不如一句准确的漏洞描述有用——因为载荷需要适配，而描述能让模型自己推理出适配。

---

## 三、三个可落地的方向（按性价比排）

### 方向 A：把 checklist 从「一次性全塞」改成「按需加载」

**问题**：4733 字符 / 7 条经验，无差别注入每一道题。Joomla 的题要读 PHP-CGI 的 `disable_functions` 轮换，Samba 的题要读 `md5($file['name'])`。

**做法**：
1. 给每条 checklist 经验打标签：`platform`（平台级，永远注入）/ `product:php` / `tech:file-upload` / `tech:cmd-injection`
2. 按 challenge 的 CVE / 产品词 / 已观测到的行为过滤
3. 平台级的（flag 在环境变量、要求实时上报）保留全量

**预期**：4733 → 约 1500 字符，且命中率提高（不用在无关条目里找）。

**风险**：过滤错了会漏掉关键提示。所以要保留「`platform` 那一层永不裁剪」作为底线。

### 方向 B：给 Worker 一个真正的交互原语

**问题**：`nc` 71 轮 / 77% 失败，`expect` 装了几乎不用。

**做法**（照抄 CTFAgent 的 EA 模块，但用 shell 工具实现，不改 agent 协议）：
在容器里放一个 `mkt-session` 小工具：
```
mkt-session start --host H --port P --name S   # 起会话，返回初始输出
mkt-session send --name S --data '...'         # 发一行，返回到目前为止的输出
mkt-session close --name S
```
底层用 `expect` 或 `socat` 常驻进程。**Worker 只需要会调它，不需要理解 pty。**

**为什么值得做**：77% 的 nc 失败不是「不会用 nc」，是「一次性管道拿不到中间反馈」。这正是论文里说的 "nc only accepts results after EOF"。

### 方向 C：把「死胡同之后必须停」从 prompt 改成契约

**问题**：我上午的 `DEADEND_DISCIPLINE` 是 prompt 层，PHP 那轮证明拦不住。

**做法**（借 Strix 的 lifecycle 工具思路）：
在黑板写入 `mark-deadend` 后，**记录当前命令序号**。若之后连续 N 条命令与 deadend 描述的路径**语义相同**（同 URL / 同参数 / 同工具），则直接触发已有的 `stagnation_command_limit` 收敛。

**这个其实已经有 80% 了**——我下午加的 `stagnation_command_limit=40` 就是这个机制，只是它不知道「哪条方向已经判死了」。加上 deadend 的路径签名就能精确化。

---

## 四、我的建议顺序

1. **方向 A 先做**——纯数据改动，不碰运行时，风险最低，且直接对应我上午判断的「kitchen sink」问题。
2. **方向 C 顺便做**——它是在我已经写好的 `stagnation_command_limit` 上加一个条件，增量很小。
3. **方向 B 最后做**——需要往镜像里加工具、改容器，涉及部署，比赛前不宜动。

**方向 B 虽然是最有意思的，但也是唯一会动到 worker 镜像的**。比赛前改镜像风险高（镜像重建、Worker 起不来就是全批失败）。建议比赛后再做。

---

## 五、一个必须记住的负面结论

**「给更多工具」不是杠杆。** SOLVED 平均 0.22 种工具，UNSOLVED 0.47 种。装了多少工具和能不能解出题，在这批数据里是**负相关**的。

真正缺的是**交互能力**（能把一条连接聊下去），不是**工具数量**。这两件事经常被混为一谈，数据把它们分开了。
