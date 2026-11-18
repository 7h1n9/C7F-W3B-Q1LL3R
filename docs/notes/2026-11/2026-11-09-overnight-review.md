# 2026-11-08 夜间无人值守：复盘

## 一、结果

**11-08 单日 19 个 accepted，历史最高**（此前单日最好 10）。队列 SOLVED **68**。

但夜间 18:00 之后实际只跑成 **5 个 Run**：

| Run | 题 | 结果 | 耗时 |
|---|---|---|---|
| `674cbdb5` | [CISCN2019 总决赛 Day1 Web3]Flask Message Board | **SOLVED** | 3 min |
| `7a4bb5e3` | [SCTF2019]Flag Shop | **SOLVED** | 5 min |
| `caff080a` | — | **SOLVED** | 25 min |
| `7fd9da91` | — | **SOLVED** | 20 min |
| `07db6150` | — | FAILED_ENGINE | 56 min |

夜间 accepted 3 道，全部是**无 CVE 常规题**（此前常规题当日新增为 0）。

## 二、机制验证：自适应预算按设计工作

`7a4bb5e3` 的完整轨迹是最干净的证据：

```
[02:09] f=0   cmds=0    RUNNING
[02:12] f=0   cmds=40   RUNNING   ← 旧逻辑在这里就收敛了
[02:14] f=17  cmds=63   RUNNING   ← 拿到跑道后爆发出 17 条 fact
[02:15] COMPLETED_SOLVED
```

40 条命令时 0 fact，按固定预算（40）该被收敛；因为此前已证明能产出 fact，它拿到了 120 的额度，随后 23 条命令里产出 17 条 fact 并解出。

**但这只是 4 道题的样本，不能据此宣称常规题能力提升。** 它能证明的是机制没有坏。

## 三、真正的失败：Docker 挂了，队列空转 3 小时

04:42 之后 16 道 PENDING 再也不动。真因不是 solver：

1. `docker ps -a` → 所有容器 `Exited (255)`，**Docker Desktop 在 ~03:50 自己退了**
2. 后端日志 → `sqlalchemy.exc.OperationalError (2003, "Can't connect to MySQL server on '127.0.0.1'")`
3. `07db6150` 在 turn 18 崩成 `FAILED_ENGINE / MUTEKI_RUNTIME_ERROR`
4. 队列保持 16 PENDING，监控脚本静默空转

### 为什么监控没发现

旧脚本 `scripts/_watch_queue.sh` 读队列用的是：

```bash
docker exec ... mysql ... 2>/dev/null
```

MySQL 没了 → 返回**空字符串** → 与「本轮没有变化」无法区分 → 脚本认为一切正常，睡到超时。

**吞掉 stderr 的健康探针等于没有探针。**

## 四、已修：自愈看门狗

新增 `scripts/watch_unattended_run.sh`，四层探针 + 对应自愈动作：

| 层 | 探针 | 失败时 |
|---|---|---|
| 引擎 | `docker info` | 拉起 Docker Desktop，等 5 分钟 |
| 数据库 | `mysql -e "select 1"` 且 `grep -qx 1` | `docker start` / `restart`，等 2 分钟 |
| 后端 | `curl /health` 精确 `200` | 清代理 json + 重启后端 |
| 队列 | 计数（空值 = 读失败） | `pending>0 且 running=0` 连续 3 轮 → `queue/start` |

**实测 4/4 全过**（故意 `docker stop` MySQL）：

```
[13:03:49] deadline: 2026-11-10 07:00        ← 跨天正确
[13:03:52] RECOVER: backend not answering; restarting it
[13:03:58] RECOVER: backend is back
[13:04:32] WARN: 17 pending with nothing running for 3 polls
[13:04:32] RECOVER: queue start -> {"state":"started"}
[13:04:49] queue: solved=68 pending=16 running=1   ← 队列被推活
```

MySQL 从 stopped 自动恢复为 `Running=true`。

## 五、顺带修掉的两个静默缺陷

### 1. `FLAG` 裸词让停滞计数器完全失效

Run `a6251f12` 跑到 **296 条命令 / 0 条 fact / 仍 RUNNING**。

`learned` 标记列表里有裸 `"FLAG"`，而 Worker 找 flag 时输出里到处是这个词（`find / -name '*flag*'`、`grep -r flag`、`cat /flag`）。154 条 tool_result 里 17 条含它，**每一条都把计数器清零**，从未累计到 40。

修法：`"FLAG"` → `"FLAG="`，补 `"FLAG{"`。判「有进展」的标记必须是**结构**，不能是**名词**。

### 2. `_FA_SHELL` 漏掉 `ssh`/`scp`/`wget`

Git CVE-2017-8386 的探针是 `ssh -p 3322 -i id_rsa ...`，一直被 `_fa_is_move` 拒绝，那道题从来没拿到过首条动作。

## 六、一次必须承认的失误

中途我加了一条「文本必须以动词开头才算命令」的判据，结果 **FA 从 2 归零、probe 从 22 掉到 6** —— 误伤了全部真实配方，已回滚。

教训：判据只能加「确定是报告」的特征，**不能要求「必须像命令」**。真实配方里有大量合法的非动词开头（`` Login `root/root` … ``）。

## 七、当前状态

- 回归 **2467 passed**（本轮新增 34 条测试）
- 队列：68 SOLVED / 16 PENDING / 1 RUNNING，正在继续跑
- 看门狗已启动，`--until 06:30`，日志 `.probe/watchdog.log`

## 八、下次长跑前

1. ~~加容器可达性看门狗~~ ✅ 已完成并实测
2. 16 道 PENDING 跑完后核对其中有多少是常规题
3. 二进制/编译类构造能力仍未做（`滑稽云音乐` 的卡点）
