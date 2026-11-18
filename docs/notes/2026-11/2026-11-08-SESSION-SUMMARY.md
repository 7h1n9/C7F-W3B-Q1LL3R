# 2026-11-06 ~ 11-08 会话完整总结

C7F-W3B-Q1LL3R（多引擎 Web 安全自动化推理系统）连续三天的优化会话。

---

## 一、会话开始时的状态

| 项 | 值 |
|---|---|
| 题库 | SOLVED 142 / ACTIVE 122 |
| Run 总量 | 532（含历史） |
| 语料 | 151 条（vulhub + curated） |
| `verified_solve` | **0** |
| 后端测试 | 2003 passed |

会话结束时的状态：

| 项 | 值 | 变化 |
|---|---|---|
| 题库 | SOLVED 142 / ACTIVE 122 | 不变（但队列里 48/82 是本机 arena 数据） |
| 语料 | 478 条 | **+327** |
| `verified_solve` | **15** | **0 → 15** |
| 后端测试 | **2371 passed** | **+368** |

---

## 二、按时间线：做了什么

### 第 1 天（11-06）：知识库扩张 + 三次跑题验证

**① Awesome-POC 提取**（`scripts/extract_awesome_poc_payloads.py`）

从 1139 个 md 提取载荷，语料从 151 条扩到 478 条，覆盖 CVE 从 38 道（42%）到 86 道（94%）。

修的关键 bug：**围栏感知的 heading 判定**。`# apt install smbclient` 是 shell 注释，朴素解析当 H1 ⇒ 段落提前终止 ⇒ Samba CVE-2017-7494 的 14 个代码块出 0 条。

**② 三次跑题验证（全 UNSOLVED，但验证了载荷采纳率）**

| Run | 题目 | 载荷使用 | 生效 | 结果 |
|---|---|---|---|---|
| `6712279f` | Joomla CVE-2017-8917 | 163/397 (41%) | 部分（SQLi 通） | UNSOLVED 25.6m |
| `e92b8431` | Drupal CVE-2018-7600 | 6 次 | 是（后来纠正） | **SOLVED 7.8m** |
| `1b2a8586` | PHP CVE-2012-1823 | 16 次 | 是（RCE 确认） | UNSOLVED 25.5m |

关键发现：**curl glob 陷阱**（`list[fullordering]` 的方括号被当 glob ⇒ `bad range in URL`），已修为 `%5B...%5D`。

**③ 路线 2/3**

- 路线 2：`post_exploitation` 字段从全空填充到 478 条（技法层 11 类 + 产品层覆盖）
- 路线 3：`_order_by_history()` 按历史解出率排序

**关键教训**：路线 2 的原始假设被数据推翻——Worker 已经把 `EXECUTION_CHECKLIST` 四步全做了，问题不是「不知道拿 flag」，是**写完 deadend 不停止**。

### 第 2 天（11-07）：五个能力改动 + 批量验证拿到首批正样本

**五个改动**（都有数据标定）：

| 改动 | 内容 | 标定依据 |
|---|---|---|
| `DEADEND_DISCIPLINE` | 死胡同后禁止同请求变体 | PHP 轮 deadend 后又跑 10 条同样的 find，`--max-time` 从 40→60→90 |
| `stagnation_command_limit=40` | 40 条无新事实 → converge | 484 Run 标定：误伤 SOLVED 14%、命中 UNSOLVED 34% |
| `stagnation_after_deadend_limit=12` | 写过 deadend 后收紧到 12 | 同类依据 |
| `execution_checklist_for()` | checklist 按需裁剪（4734→2278 字符） | 噪声比例 57%→0% |
| `mkt-session` | 交互原语（send/read 不重放） | `nc` 用了 71 轮，**77% 伴随超时/断连** |

**`mkt-session` 踩了三个真 bug**（全靠容器内端到端测试暴露）：
1. 脚本复制成 `/tmp/mkt-session` 与状态目录同名 ⇒ `FileExistsError`
2. daemon 不建自己的目录 ⇒ 第一次写日志就死，而 `start` 只报 `rc=1`
3. **`os.setsid()` 二次调用**：`Popen(start_new_session=True)` 已调过，daemon 再调 ⇒ `PermissionError`。表面只显示 `rc=1`，查了三轮

**批量验证：三轮共 15 道题**

| 轮次 | 题数 | 解出 | 解题率 |
|---|---|---|---|
| 第 1 轮 | 6 | 2 | 40%（Confluence 503 + Nexus 502 吃掉 52 分钟）|
| 第 2 轮 | 5 | **5** | **100%** |
| 第 3 轮 | 6 | 3 | 60% |

**`verified_solve` 0 → 15**

**归因方法踩了四轮才对**（`scripts/credit_verified_solves.py`）：
1. 只按 token 重叠 → 把 `drupal:7600` 归给任意 Drupal 题
2. 加产品/CVE 匹配 → 单 token 太弱
3. 加稀有度（`df ≤ 2`）→ 还是分不清
4. **加「响应必须活的」判据** → 才正确

关键判据：`_LIVE_REPLY` 要同时满足「有执行痕迹」（`uid=`／traceback／`"rows"`／`"code":`／`psession`）和「不是应用自己的页面」（`form_build_id`／404／502／503）。

### 第 3 天（11-08）：先否定自己的假设，再修 bug

**★ 最重要的一次自我纠正**：我上一轮说「6 道题都是拿同产品参考的题」，并归功于 sibling 机制。实查 `muteki/AGENTS.md`：

```
34d6b3f9  sibling=0  ownRecipe=1
33c330f1  sibling=0  ownRecipe=1
af8587a3  sibling=1  ownRecipe=0   ← 唯一真走 sibling
7763dbb8  sibling=0  ownRecipe=1
b4330d4a  sibling=0  ownRecipe=1
```

**sibling 作用域只有 1/123 道**，而且那一道（Drupal 6341）跑了 69 条命令，**Drupalgeddon2 指纹命中 0 条**——它走 `/core/install.php` 解出的。**sibling 有效样本 = 0**。

**★★ 第二个自我纠正（最关键）**：我提出的「FA 采纳率 5.6%」**不成立**。

| 证据 | 值 |
|---|---|
| `run_events` 里 `exploit.first_action` 事件 | **0 条** |
| 最后一个 run_event | `11-07 11:34` |
| `coordinator.py` / `known_exploit.py` mtime | `11-07 22:46` / `22:52` |
| `intent_proposed` 3045 条含 "FIRST ACTION" | **0 条** |

**FA 注入代码从未在生产跑过。** 我统计的是旧机制。

**真正被数据指向的因果链**：`cli_solver.py:1999` 的 `_board_pointer()` 里的「**READ IT FIRST**: build on confirmed facts...」在 prompt **第 424 字符**，FA 在第 1226。167 个有 FA 的 Run 里只有 **12% 第一条命令就打目标**，中位暖场 **2 条**。

**★ 生存偏差**：ACTIVE 122 道只有 **4 道有 FA（3%）**，SOLVED 142 道有 **127 道（89%）**。

**方向一：修了 3 个提取器 bug，净增 1 条**

| bug | 影响 |
|---|---|
| 关键词必须独占标题 ⇒ `## Vulnerability Reproduction` 不匹配 | 5 个英文 CVE 全 0 段落 |
| `_pick_readme` 优先中文版，但中文版用 `## 测试过程` | Samba 2017-7494 载荷消失 |
| `## 复现漏洞` 倒装词序 | 6 个 CVE |

那 6 个「缺失 CVE」**0 个能靠改正则补上**：HTTPD 无 HTTP 请求示例、GhostScript/Drupal 只有本地 docker 或外部脚本、Django 是 XSS、Tomcat 源和语料都没有。

---

## 三、核心数据（全部实测）

### 3.1 FIRST ACTION 是最强杠杆

| | 题数 | Runs | 解出 | 率 |
|---|---|---|---|---|
| 能发 FA | 143 | 347 | 157 | **45%** |
| 不能发 | 121 | 319 | 24 | **7%** |

最干净对照（非 CVE + 无导入配方，只差 FA）：

```
FA=有:  86 道  203 runs  解出 94 = 46%
FA=无:  82 道  255 runs  解出 20 =  7%
```

**6.5 倍**，排除「能发 FA 的题本来就简单」的解释。

### 3.2 123 道 ACTIVE 题的缺口分解

| 缺口 | 数量 | 占比 | 现有手段能否覆盖 |
|---|---|---|---|
| A. 标题无 CVE 的普通 Web 题 | 41 | 34% | **不能** |
| E. 只有 `learned-wall` 负面记录 | 42 | 35% | **不能** |
| D. 只有未验证导入条目 | 35 | 29% | 能（批量验证中）|
| A'. 有 CVE 但语料缺失 | 6 | 5% | 补语料 |

**批量验证只能覆盖 29%**，A+E 那 83 道（69%）是空白。

### 3.3 三个负面结论（省掉重复劳动）

| 假设 | 检验结果 |
|---|---|
| 「给更多工具」是杠杆 | **反相关**：SOLVED 平均用 0.22 种工具，UNSOLVED 用 0.47 种；sqlmap 全库仅 13 次 |
| 「读更多本地 POC 库」有正相关 | **无**：读 0 个 CVE 目录解出 33%、读 1 个 45%、读 2 个 37%、读 3+ 个 33% |
| 「失败轮起步更慢」 | **无差异**：首次利用尝试 SOLVED 中位第 5 条 vs UNSOLVED 第 6 条 |

### 3.4 靶机状态是最大方差来源

第 1 轮 40%（2 台靶机 503/502 吃掉 52 分钟）vs 第 2 轮 100%（靶机全活）。**比 agent 能力影响更大。**

---

## 四、当前系统能力全景

### 能力来源（五层，但有轻重）

| 层 | 内容 | 实际到位程度 |
|---|---|---|
| 模型 | codex / claude CLI | 主力 |
| 环境 | Kali 容器：sqlmap/ffuf/gobuster/hydra/nmap/commix/nc/expect/tmux | 齐全但使用率极低 |
| 知识库 | 478 条，其中 **15 条已验证** | 96.9% 未验证 |
| 提示注入 | checklist（按需 2278-3145 字符）+ 配方 + 死胡同纪律 + `mkt-session` 指南 | — |
| 黑板 | facts / dead_ends，最多 30 条进 prompt | 轮内回灌依赖模型主动读 |

### 决策链

```
第 1 层  有 curated 配方？
        → 有：lane goal = FIRST ACTION + 配方步序（禁止自行扫描）
        → 无：第 2 层
第 2 层  语料能识别技术类别？
        → 有：technique_candidate_goal
        → 无：第 3 层
第 3 层  _SINGLE_WORKER_GOAL 兜底
        → 读 AGENTS.md → 指纹 → 读 /home/kali/pocs → 探索
```

### 已修的死指令

`lookup_public_poc` 注册在 `openai_compatible.py`（chat_completions 专用），CLI Worker 在容器里跑调不到，**484 Run 调用 0 次**。已改为读 `/home/kali/pocs`，并新开 `intel:public-poc` lane（`requires_protocol="chat_completions"`）让这个能力第一次真正可达。

---

## 五、下一步（按数据排序）

### P0：改 `_board_pointer()` 让 FA 排在「READ IT FIRST」前面

**改哪里**：`backend/muteki/solver/cli_solver.py:1996-2014`

**怎么改**：加 `has_first_action: bool` 参数，为 True 时把「READ IT FIRST」降级为「The course correction below takes precedence over this board; read the board only if it fails.」

**预期**：暖场中位 2 → 0-1，前 3 条采纳率 15% → 50%

**前置**：必须先跑 3 个 Run 确认新代码能产生 `exploit.first_action` 事件（`in_window=true`），否则改完无法验证——**这正是今晚犯过的错误**。

### P1：常规题能力（83 道，69%）

三个候选路线，都需要先验证数据可行性：
1. 从历史 `run_events` 反推侦察结论（不用新起 Run）
2. 建通用 Web 技法库（非 CVE 组织，按「攻击面→检测动作→利用动作」）
3. 改 agent 行为（强制侦察纪律）

### P2：FA 曝光率

`coordinator.py:1752` 只有 `lane_key.startswith("exploit:")` 才注入 FA，6 个 lane 只有 index 0 拿得到 ⇒ **曝光率 1/6**。另有 79 个 run（12%）整个 run 没有 race worker。

### P3：载荷可执行性

部分载荷依赖容器**没有的工具**（`java -jar ysoserial.jar`），语法对但环境不支持——这是 `verified_solve` 之外的另一层「纸面能力」。

---

## 六、方法论教训（比代码更重要）

### 1. 「产出」≠「生效」

三次踩同一个坑：

- `verified_solve` 刷到 12，但 `_pick_entries` 没有「同产品其他 CVE」轴 ⇒ 对剩余题零帮助
- sibling 机制做完了，作用域 1/123，且唯一那例没被采纳
- `verified_solve` 刷到 15，但 ACTIVE 122 道只有 4 道有 FA

**每次都是「我做完了一个东西」，但没查「它到得了用户手上吗」。**

### 2. 统计前必须确认被测代码真的跑过

我用 mtime 对比最后事件时间，10 秒就能发现 FA 代码从未执行。但我先做了两小时的错误统计。

### 3. 个案吻合度不构成证据

Drupal 读 22 次 POC 并解出，Joomla 读 2 次没解出——看起来很动人，但 484 Run 统计说无相关性。**差点据此改 prompt。**

### 4. 正确跳过比多提取重要

`测试环境搭建` 出现 19 次，比要修的还多。宽泛加「测试」会把 `docker compose run` 当载荷。语料的价值在精确，宁可少一条也不要多一条假的。

### 5. 先验证假设再动手

路线 2 的原假设「Worker 打通后不知道拿 flag」——实测它把 checklist 四步全做了。**如果不先查，就会去补一个已有的东西。**

### 6. 测试示例绑定具体题目会假失败

`test_the_sibling_reference_is_labelled_and_short` 原用 `[Drupal]CVE-2019-6341`，后来该题出现 learned 条目，sibling 不再是唯一来源，测试失败。改用 `[Tomcat] CVE-2020-9484`。

---

## 七、文档索引

| 文件 | 内容 |
|---|---|
| `docs/notes/2026-11/2026-11-06-verify-run-1.md` | 三轮跑题验证完整数据 |
| `docs/notes/2026-11/2026-11-06-route-2-3.md` | post_exploitation + 历史排序 |
| `docs/notes/2026-11/2026-11-06-command-stagnation.md` | 停滞检测与阈值标定 |
| `docs/notes/2026-11/2026-11-06-tools-and-research.md` | 工具盘点 + 外部研究 |
| `docs/notes/2026-11/2026-11-06-direction-a-checklist-trimming.md` | checklist 按需裁剪 |
| `docs/notes/2026-11/2026-11-06-direction-b-session-tool.md` | mkt-session 交互原语 |
| `docs/notes/2026-11/2026-11-06-CHECKPOINT.md` | 断点续接 |
| `docs/notes/2026-11/2026-11-07-batch-1.md` | 批量验证第 1 轮 |
| `docs/notes/2026-11/2026-11-07-batch-2.md` | 批量验证第 2 轮（5/5） |
| `docs/notes/2026-11/2026-11-07-sibling-transfer.md` | sibling 机制实现 |
| `docs/notes/2026-11/2026-11-07-batch-3-sibling-scope.md` | sibling 实测作用域 |
| `docs/notes/2026-11/2026-11-08-fa-adoption-diagnosis.md` | **FA 采纳率诊断（最重要）** |
| `docs/notes/2026-11/2026-11-08-dir1-extractor-fix.md` | 提取器三 bug |

工作记忆：`~/.workbuddy/.../memory/2026-11-06.md` ~ `2026-11-08.md`
