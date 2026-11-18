# 验证的价值传递：12 条 verified_solve 为什么没帮到剩下的题

2026-11-07 17:30。

---

## 一、先验证了一个不该跳过的假设

上一轮结束时我说「`verified_solve` 从 0 → 12」，但没验证这些验证**有没有用**。今天先查了这个。

结果很难看：

| 事实 | 数值 |
|---|---|
| 已验证条目对应题目的状态 | **11/12 已经 SOLVED** |
| 剩余 126 道 ACTIVE 题中，产品名与已验证条目重叠 | 8 道 |
| 这 8 道里，实际拿到同产品已验证配方的 | **0 道** |

原因：`_pick_entries` 只按 **box_names / CVE / product+technique** 三个轴匹配，**没有「同产品的其他 CVE」这条轴**。

所以那 12 条验证的价值只停留在「已解出的题」，对剩下 126 道**零帮助**。

---

## 二、但这不等于白干——Worker 早就在做这件事

查了近 10 轮的命令：**9 轮主动读了 `/home/kali/pocs` 里的 vulhub / Awesome-POC**。

它一直在手工 grep 整个产品目录，包括不适用的 CVE。也就是说：

**给它一条「同产品已验证配方」的参考，是在替代它已经在做的低效行为，不是新增负担。**

---

## 三、实现时踩到一个真实的坑

第一版直接把同产品条目**完整渲染**进 block。结果：

```
[Weblogic]Something Else → 3500 字符，全在讲 CVE-2017-10271 的 wls-wsat
（具体到 servers/AdminServer/tmp/_WL_internal/bea_wls_internal/9j4dqk/war/pwn.jsp）
```

这正是 `_pick_entries` 注释里记录过的失败：

> 一个 Worker 在 `[Weblogic]SSRF` 上把回合花在了 CVE-2018-2894 的 ws_utc 链上。**无关配方比没有更糟——它把回合引向错误的端点。**

我差点重现了这个 bug。修法：**只给摘要，不给配方体**。

```python
# 只渲染三行：标题 + 技法 + 摘要（≤260 字符）
- **WebLogic WLS-WSAT XMLDecoder RCE (CVE-2017-10271)** — XMLDecoder deserialization RCE
  WebLogic 的 WLS Web Services 把 SOAP 请求体交给 XMLDecoder 解析...
```

3500 → **1151 字符**，且不含任何载荷模板。

---

## 四、设计上的四条硬约束

1. **不进 `_pick_entries`** —— 只由 `sibling_verified_recipes()` 单独返回，不占配方槽位
2. **不占 FIRST ACTION** —— `exploit_first_action()` 完全不看它
3. **只在「本 CVE 无配方」时出现** —— 有自己配方的题永远优先自己的
4. **明确标注不是本 CVE 的配方** —— header 直接写 `NOT as this box's plan`

第 4 条是关键。原文：

> There is no recipe for this box's own CVE. What follows produced an accepted
> flag on this platform for *another* CVE of the same product. Use it as a
> reference for where this product keeps its attack surface and what a working
> chain looks like -- **NOT as this box's plan**. The endpoint, parameter and
> payload belong to the other CVE and will not transfer unchanged; confirm
> anything you borrow.

---

## 五、效果

| 靶机 | 结果 |
|---|---|
| `[Drupal]CVE-2019-6341` | 拿到 Drupalgeddon2（7600）的参考，579 字符 |
| `[Weblogic]Something Else` | 拿到 WLS-WSAT（10271）的参考，1151 字符 |
| `[Couchdb]CVE-2017-12636` | 有自己的配方 → 不触发 siblings |

---

## 六、改了 1 条既有测试

`test_product_or_technique_alone_is_not_enough` 原断言：

```python
assert known_exploit_block(_challenge("[Weblogic]Something Else")) == ""
```

我的改动让它不再为空。**这条测试的意图是对的**（产品名单独不足以给配方），但我的改动没有违反它——siblings 明确不是配方。

改成检查正确的性质：

- 必须说「for a different CVE」
- 不能以「for THIS CVE」开头
- 必须 <1500 字符（完整渲染会到 3500）
- 不含载荷体

`[Something]SSRF` 仍然断言为空（无产品名，不匹配任何东西）。

---

## 七、验证

| 项 | 结果 |
|---|---|
| 全量回归 | **2312 passed**（2308 → 2312） |
| 新增测试 | 4 条（sibling 相关） |
| 后端 | 已重启 |

---

## 八、这件事的教训

**「产出」不等于「生效」。** 我上一轮把 `verified_solve` 从 0 刷到 12，报告里写了「覆盖 12 个 CVE」，听起来是进展。但实际上这 12 条对剩下 126 道题一点用都没有——因为匹配轴里根本没有能把它们送出去的路。

如果我没查这一步，接下来继续刷到 30 条、50 条，也只是在给已解出的题打更多标记。

**下一步该做的**：跑一轮，看这 8 道拿到同产品参考的题，解题率是否高于基线 24%。如果没提升，说明「参考摘要」这个形式也不够，得换思路。
