# 方向 A：checklist 从「一次性全塞」改成「按需裁剪」

2026-11-06 16:20。

---

## 一、问题

`EXECUTION_CHECKLIST` 是逐条长出来的——每一条都是某次 Run 失败后追加的。到 2026-11-06 已经 **4734 字符 / 7 条**，而其中 4 条只对**特定类型**的靶机有意义：

| 条目 | 字符 | 谁需要 |
|---|---|---|
| Flag hunt, in this order | 713 | 所有题 |
| Confirm before pivoting | 119 | 所有题 |
| Stop condition (do not loop) | 621 | 所有题 |
| A normal page ≠ failed injection | 424 | PHP 类 |
| PHP entry points, two traps | 441 | PHP |
| Command injection: get the output | 747 | 命令注入 |
| Stored-name derivation | 840 | 上传 / LFI |

**Joomla 的题被告知要轮换 `disable_functions`；Samba 的题被告知 `md5($file['name'])` 的推导。**

这两条都不是错的，但在那些靶机上**无法执行**。代价不是模型会违背——而是**真正重要的那一行在 4.7k 字符里更难被发现**。

这是 Strix 明确禁止的 "kitchen sink agent" 形态，也和 CTFAgent 论文的发现相反（论文用两阶段 RAG 按阶段只给 2 条）。

---

## 二、做法

### 保守的第一原则：不动原常量

`EXECUTION_CHECKLIST` 保持**全量不变**——已有 81 条测试用 `.index()` 和 `in` 索引它。新增 `execution_checklist_for(name, description, technique)`，在读取时按标签裁剪。

这样向后兼容，且出问题时能立刻回退（把 runtime 那一行换回 `EXECUTION_CHECKLIST`）。

### 三层分类

```python
_PLATFORM_LESSONS = ("Flag hunt", "Confirm before pivoting", "Stop condition")
_LESSON_TRIGGERS = (
    ("normal page is not a failed injection", ("php", "cve-2012-1823", ...)),
    ("PHP entry points only", ("php", "cve-2012-1823", ...)),
    ("Command injection: get the output", ("ping", "cmd", "rce", ...)),
    ("When a stored name or path is derived", ("upload", "file", "lfi", ...)),
)
```

- **平台级**：永远注入，无条件
- **条件级**：命中 trigger 词才注入
- **未分类**：默认保留（宁可多给，不能静默丢）

匹配对象是 `name + description + technique` 的 lowercase 拼接——在 Worker 跑任何命令之前，只有这些是可靠的。

### 尾部 section 永不裁剪

「Report findings WHILE you work」不是 bullet 条目，是独立 section。它是平台级的（一个不发布任何东西的 Run 无法引导自己下一轮），所以在任何裁剪下都保留。

---

## 三、效果

| 靶机 | 裁剪后 | 条目数 | 降幅 |
|---|---|---|---|
| `[Joomla]CVE-2017-8917` | **2278** 字符 | 3 | **-52%** |
| `[Samba]CVE-2017-7494` | **2278** 字符 | 3 | **-52%** |
| `[GXYCTF2019]Ping Ping Ping` | 3026 | 4 | -36% |
| `[0CTF 2016]piapiapia` | 3119 | 4 | -34% |
| `[Drupal]CVE-2018-7600` | 3119 | 4 | -34% |
| `[PHP]CVE-2012-1823` | 3145 | 5 | -34% |

分类正确性核对：
- Joomla 不再收到 `disable_functions`
- Samba 不再收到 `md5($file['name'])`
- PHP 拿到两条 PHP 专属
- Ping Ping Ping 拿到命令注入那条
- piapiapia / Drupal 拿到上传派生那条

---

## 四、剩余体量说明

裁剪后仍有 2278 字符（平台级 3 条 + 报告 section）。这已经是**不再缩减的下限**：

- Flag hunt 713 字符——四步 flag 搜索，每步都带具体命令
- Stop condition 621 字符——含一条实测记录（PHP 那题「0 solves, three Runs wasted」）

这两条都是**防止已观测失败**的，不能为省字符删掉。真要再降，只能改写文案而不是删条目——那是另一件事，且风险高于收益。

真正的收益在**噪声比例**：从 4 条无关 / 7 条（57% 噪声）降到 0 条无关 / 3 条（0% 噪声）。

---

## 五、验证

| 项 | 结果 |
|---|---|
| 全量回归 | **2300 passed**（2284 → 2300） |
| 新增测试 | `test_checklist_trimming.py` 16 条 |
| 原常量 | 4734 字符 / 7 条，未改动 |
| 后端 | 已重启 |
| 前端 | 已重启（Vite 5173） |

测试覆盖：全量常量不变、7 条可完全拆分、未匹配题只拿平台级、Joomla 无 PHP 陷阱、Samba 无上传派生、5 个匹配题各自拿到对应条目、报告 section 永远保留、header 永远存在、裁剪确实缩小 40%+、technique 参数可单独选中、空输入安全、Stop condition 处处存在。

---

## 六、待做（方向 B，比赛后）

**`mkt-session` 交互原语**——给 Worker 一个「发一条、收一条」的会话，替代 `printf | nc` 这种一次性管道。

数据支撑：`nc` 在 71 轮里被用，**77% 伴随超时或断连**；`expect` 装了却只有 8 轮用到。CTFAgent 论文（CCS'25）把这条列为 EA 模块的核心，报告 Intercode-CTF 39→73/100。

**为什么比赛前不做**：需要往 worker 镜像里加工具并重建容器。镜像起不来就是全批失败。
