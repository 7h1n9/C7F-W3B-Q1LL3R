# 2026-11-07 第三轮：sibling 机制实测 —— 作用域只有 1 道，且未被使用

跑完 6 道「疑似能拿同产品参考」的题。结论比预期差，而且要纠正上一轮的判断。

## 一、跑的结果

| 题 | Run | 时长 | 结果 | 配方来源 |
|---|---|---|---|---|
| `[ElasticSearch]CVE-2015-3337` | `34d6b3f9` | **166s** | SOLVED | **自己的** `awesome-poc:cve-2015-3337` |
| `[ElasticSearch]CVE-2015-5531` | `7763dbb8` | 583s | SOLVED | **自己的** `vulhub:elasticsearch:cve-2015-5531` |
| `[Drupal]CVE-2019-6341` | `af8587a3` | 928s | SOLVED | **sibling**（Drupalgeddon2） |
| `[Couchdb]CVE-2017-12636` | `de40b75f` | 963s | SOLVED | **自己的** `vulhub:couchdb:cve-2017-12636` |
| `[Python]PIL-CVE-2017-8291` | `33c330f1` | 1551s | UNSOLVED | 自己的 `awesome-poc:cve-2017-8291` |
| `[Drupal]CVE-2018-7602` | `b4330d4a` | 1423s | UNSOLVED | 自己的 `vulhub:drupal:cve-2018-7602` |

3 解出 / 2 未解 / 1 超时未解。**解题率 3/5 = 60%**。

## 二、纠正上一轮的错误判断

上一轮我写：「**6 道题都是拿到同产品参考的题**」，并把 3337、6341、5531 都算成 sibling 的功劳。

**这是错的。** 实际检查每轮的 `muteki/AGENTS.md`：

```
34d6b3f9  sibling=0  ownRecipe=1   ← 有自己的配方
33c330f1  sibling=0  ownRecipe=1
af8587a3  sibling=1  ownRecipe=0   ← 唯一真走 sibling 的
7763dbb8  sibling=0  ownRecipe=1
b4330d4a  sibling=0  ownRecipe=1
```

只有 `af8587a3` 真的拿到了 sibling。3337 和 5531 解出，是因为它们**自己就有 CVE 条目**（在导入语料里），跟 sibling 无关。

## 三、sibling 机制的作用域：1/123

精确统计 123 道 ACTIVE 题：

| 分类 | 数量 |
|---|---|
| 有自己的配方 | 79 |
| **只靠同产品参考（sibling）** | **1** |
| 两者都没有 | 43 |

唯一的 sibiling 受益题是 `[Tomcat] CVE-2020-9484`，参考 `cve-2017-12615-tomcat-put-jsp`。

**为什么这么少**：`_pick_entries` 按 CVE 精确匹配，而导入语料 478 条覆盖 400 个 CVE，本身命中率就高——79/123 有自己的配方。sibling 只在「产品名能识别、但本 CVE 没条目」时才补位，这个交集极小。

## 四、而且那一例也没被使用

`af8587a3`（Drupal 6341）虽然拿到了 sibling，但检查它 69 条命令：

```
命中 Drupalgeddon2 指纹（user/register、element_parents、drupal_ajax、
                        mail[#post_render]）: 0 条
```

它是靠 `/core/install.php` 走安装流程解出的，**完全没照 sibling 的提示走**。

所以 sibling 机制的**有效样本是 0**。它确实投递到了（`AGENTS.md` 里能看到），但 Worker 没采纳。

## 五、但这个机制仍然值得保留

理由来自另一组数据。测算「如果 478 条导入条目全部验证过」的上限：

```
基线（15 条已验证）:  能发 FIRST ACTION 的题  5/123
全部验证            :  能发 FIRST ACTION 的题  38/123
                                  净增 33 道
```

**118 道 ACTIVE 题现在拿不到可执行的第一步。** 验证是唯一能改变这个的手段——因为 `exploit_first_action` 只对 `verified_solve` 或 `trust_level=reviewed` 的条目放行。

而 sibling 是这个机制里唯一能「跨题传递」的通路。作用域小（1 道），但当那 33 道的验证做完后，同产品题之间会形成网——一个 CVE 验通，同产品其他 CVE 至少能看到「这产品有可走的路」。

## 六、归因结果

`verified_solve`：**12 → 15**

新增：
- `awesome-poc:cve-2015-3337`（run `34d6b3f9`）
- `vulhub:elasticsearch:cve-2015-5531`（run `7763dbb8`）
- `vulhub:couchdb:cve-2017-12636`（run `de40b75f`）

`drupal:cve-2018-7602` 未归因（未解出）。`drupal:cve-2019-6341` 没有对应条目（它本来就没有自己的配方）。

## 七、测试修正

`test_the_sibling_reference_is_labelled_and_short` 原来用 `[Drupal]CVE-2019-6341`，现在失败了——因为那道题后来出现了一条 learned 条目，sibling 不再是它的唯一来源。

改用 `[Tomcat] CVE-2020-9484`（唯一真走 sibling 且无 learned 竞争的题）。这是测试示例的选择问题，不是功能回归。

## 八、诚实的结论

1. **sibling 不是杠杆**，作用域 1/123，且唯一样本未采纳。
2. **真正在起作用的是「自己 CVE 的配方 + 批量验证」**：3337 用 166 秒解出，靠的就是一条 `awesome-poc` 条目。
3. **验证的上限很值**（5→38），但需要几百轮 Run——平均 15 分钟/题，33 道增量约需 30 道验证成功，按 50% 解题率是 60 轮 ≈ 15 小时。
4. **本轮 60% 解题率高于基线 24%**，但样本只有 5 道，且没有对照组，不能归因给任何单一改动。

全量回归 **2312 passed**。
