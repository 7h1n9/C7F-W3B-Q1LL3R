# L1 第一轮判断质量：实现与基线（2026-11-05 晚）

承接 `docs/notes/2026-11/2026-11-05-L1-checkpoint.md`。断点已续上，指标已校准，基线已落地。
**A/B 实验还没跑**，这是下一步。

---

## 一、指标为什么是「首次利用步数」而不是解出率

解出率太粗：噪声大、需要靶机存活、要很多 Run 才动一点。而被研究的失败形态是
"**握着证据却没去打**"，它的可观测形式就是**第几步第一次出现利用形态的命令**。

三个衍生指标：

| 指标 | 含义 |
|---|---|
| `first_exploit_step` | 首次出现利用形态命令的步序号（**None = 全程没试过**） |
| `installer_before_exploit` | 是否在首次利用之前先去驱动靶机自身的安装向导 |
| `gap_after_first_exploit` | 首次利用到第二次利用的间隔，衡量「打一枪就跑了」 |

`None` 与 `0` 严格区分：前者是"从未尝试"，后者会把它算成"第 0 步就打了"。

## 二、检测器改了三版，前两版都是错的（记录以防重犯）

| 版本 | 判据 | 结果 | 错在哪 |
|---|---|---|---|
| v1 | 端点关键词 + `; id` 链式 | Confluence 判在第 **2** 步 | `cd /x && curl ...` 是 Worker 自己在编排步骤，不是注入 |
| v2 | 只认载荷特征字符 | Confluence 第 51 步（对了），但**已解 Run 利用步中位=0** | 已解必然利用过，中位 0 说明检测器瞎了 |
| v3 | 载荷 + **执行形态**（跑 exploit.py / sshpass / ysoserial / jenkins-cli）+ **读完整命令** | 已解 81% 能识别 | 可用 |

v3 的关键修正有两处：
1. **执行形态也算利用**。`72d923d1` 用 `sshpass -p vulhub ssh` 拿 root；`65d323f2` 写
   `exploit.py` 再跑，载荷根本不在命令行上。
2. **必须读完整命令**，不能只看首行。多数已解 Run 在 heredoc 里构造利用
   （`cat > exploit.sh <<'EOF'` 后面几行才是载荷）。只读首行时已解识别率 56%，
   读完整命令后 81%。

### 内置有效性自检

`summarise()` 会检查"已解 Run 里有多少条识别出利用"，低于 70% 直接告警
**基线数字不可用于 A/B 对比**。v2 就是被它拦下来的——否则我会拿着一份自信但无意义的基线。

> 教训：一个"看起来很好"的指标，先证明它能识别已知的正样本，再用它判断别的。

## 三、基线（200 条 Run，43 已解 / 118 未解）

| | 已解 | 未解 |
|---|---|---|
| n | 43 | 118 |
| **首次利用步中位** | **10** | **19** |
| 利用步数中位 | 5 | 5.5 |
| 先装环境占比 | 2% | 4% |
| 首→次间隔中位 | 3 | 4 |
| 从未出现利用 | 18% | 19% |

### 这组数字能说什么、不能说什么

**能说**：已解 Run 里"利用"来得更早（10 步 vs 19 步），方向是对的——第一轮判断质量
确实体现在"多早开始打"上。

**不能说**（必须记下来，防止后面自我欺骗）：
- 差异是**中位数对中位数**，没做显著性检验，n=43 也不大；
- `先装环境` 全库只有 6 条（2%+4%），**不是普遍现象**。Confluence 那种 88 步装环境的
  是极端个例，不能当主要矛盾讲；
- 这批全是 `off/unknown` 臂（L1 今天才写，历史 Run 都没有这个块），所以**这是对照组
  基线，不是实验结论**；
- 18%/19% 的 Run 全程没有一次利用形态命令 —— 这部分要么在纯侦察，要么手法没被检测器覆盖。

## 四、已实现（代码）

1. `backend/app/services/known_exploit.py` — `FIRST_MOVE_DISCIPLINE`（1247 字符，3 条约束）
2. `backend/app/core/config.py` — `first_move_discipline: bool = True`（`APP_FIRST_MOVE_DISCIPLINE`）
3. `backend/app/services/workspace.py` — 拼接顺序 + **`getattr` 安全取值**
   （测试替身 `SimpleNamespace` 没有新字段，直接属性访问会让建 workspace 崩掉，
   一个测量开关不该能把实验变成故障）
4. `scripts/measure_first_exploit.py` — 指标脚本，`--run <前缀>` 单题、`--compare` 分臂
5. `backend/tests/test_first_move_metric.py` — 16 条

验证：
```
AGENTS.md (L1 on):  14 行，新块在第 5-8 行，head-50 必覆盖
检测器有效性：35/43 已解 Run 识别出利用（81%）
全量回归：2201 passed（基线 2185 + 新增 16）
```

## 五、下一步：A/B 实验

```bash
# 对照组（关掉 L1）起后端
cd "D:/desktop/毕业设计/C7F-W3B-Q1LL3R"
env -u PYTHONPATH APP_FIRST_MOVE_DISCIPLINE=0 BACKEND_PORT_OVERRIDE=8000 bash scripts/_run_backend.sh
# 实验组（默认开）另起一个端口
env -u PYTHONPATH BACKEND_PORT_OVERRIDE=8001 bash scripts/_run_backend.sh
```

挑题原则：
- 优先**已解出过**的题（有 ground truth，能验证检测器）
- 避开已知坏靶机（`[AppWeb]CVE-2018-8715` 已从队列删）
- ⚠️ **跑 Run 期间不要改后端代码** —— 改文件触发重载会把 Run 打断成 `CANCELLED`
  （昨天 `Run 3` 就是这么废掉的）

判定看 `first_exploit_step` 左移 + `installer_before_exploit` 占比下降，
用 `scripts/measure_first_exploit.py --compare` 分臂对比。

## 六、遗留（未做）

- **靶机自身故障识别**：39 条长跑里 6 条是 setup 向导 500 / `HoldingUntilStarted`
  （`db798b52` Nexus、`94200708` Jenkins、`5cb19393` Confluence、`79ca6b1a` ActiveMQ、
  `f425b23a` PHP、`6246355f` 996game）。应自动标 broken 跳过，别计入 agent 能力。
- L2（TDA 难度估计 + 分支预算）排在 L1 验证之后。
- L3 训练/RLVR **必须等 `fresh_reproduction_verified` 不为 0**（现仍为 0）。
