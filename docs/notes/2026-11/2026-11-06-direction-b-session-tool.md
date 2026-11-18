# 方向 B 落地：mkt-session 交互原语（不改镜像）

2026-11-06 20:20。比赛前完成，**没有动 worker 镜像**。

---

## 一、为什么不用重建镜像

原计划要往镜像里加工具，那是最危险的做法：镜像出错就是全批失败。

实际查证后找到一条零风险路径：**`<workspace>` 是 bind mount 到 `/home/kali/workspace`**（`backend/muteki/solver/container_exec.py:12` 明确写了）。所以只要把脚本写进 workspace，容器里立刻可执行，**不需要任何镜像改动**。

容器内已有的东西（实测，不需要装）：
- `python3 3.13.14`
- `socat`、`expect`、`nc`、`tmux`
- `python3 -c 'import pty,socket'` → ok

---

## 二、问题回顾（数据）

484 个 Run 里：

| 交互形态 | 用过的 Run | 伴随超时/断连 |
|---|---|---|
| heredoc 喂输入 | 202 (41%) | 149 |
| **nc 反弹/连接** | 71 (14%) | **55 (77%)** |
| **expect 驱动** | 8 (1%) | 6 |

`nc` 77% 失败的原因不是「不会用 nc」，是**形态不对**：一条 shell 命令把所有输入一次性发完，只在对端关闭时才返回。Worker 在手工重建这个缺失层——`printf 'GET / HTTP/1.1\r\n...' | nc`、`tmux new-session -d -s listener 'nc -lvnp 8888'`。

CTFAgent（CCS'25）把这条列为 EA 模块的核心：静态 CLI 工具「只在 pipeline 输出 EOF 后才接受结果」，所以 agent 无法「发一个 payload、看一个回应」。

---

## 三、实现

`scripts/mkt-session.py`，单文件、无依赖、纯标准库。

### 架构

一个 daemon 独占 socket，双向都走 Unix socket：

```
worker ──> mkt-session send ──unix──> daemon ──tcp──> target
worker <── stdout           <──unix── daemon <──tcp── target
```

daemon 同时把所有收到的字节**追加到日志文件**，所以：
- 每次 `send` 是读文件，不是新建连接
- `--offset` 可以分页读，不会重复回放
- 会话活过 daemon 也留下证据

### 命令

```bash
python3 ./mkt-session start --host H --port P --name s   # 连接，打印 banner
python3 ./mkt-session send  --name s --data 'id'         # 发一行，只回新增输出
python3 ./mkt-session read  --name s                     # 只读不写
python3 ./mkt-session close --name s
python3 ./mkt-session list
```

状态在 `/tmp/mkt-session`（`MKT_SESSION_DIR` 可覆盖），不污染 run workspace。

---

## 四、三个真实 bug（都是跑测试才暴露的）

### 1. 命名冲突
脚本被复制成 `/tmp/mkt-session`，**和状态目录 `/tmp/mkt-session` 同名**，`mkdir` 直接 `FileExistsError`。

### 2. daemon 不建自己的目录
`_base()` 只在客户端路径调用，daemon 直接 spawn 不经过它。缺目录时 daemon 在第一次写日志就死，而 `start` 只报「daemon exited rc=1」——**看上去像连接被拒，实际是父目录不存在**。

### 3. `os.setsid()` 二次调用报错（最关键）
```
PermissionError: [Errno 1] Operation not permitted
```
`subprocess.Popen(start_new_session=True)` **已经调过 setsid**，daemon 里再调一次就炸。这个错误表面只显示成 `rc=1`，连查三轮才定位。

修法：daemon 不再 detach，只设 signal handler。

---

## 五、验证

`scripts/test_mkt_session.py` 在容器内跑端到端：

```
--- start ---
[mkt-session] 'e2e' open pid=10 offset=16
WELCOME
login:
--- send id ---
got:id
next:
--- send whoami ---
got:whoami
next:
--- list ---
  e2e                      open  48 bytes
--- close ---
[mkt-session] 'e2e' closed

OK: banner on start, per-command replies, no replay
```

关键点：`send whoami` 返回的是 `got:whoami`，**没有重放前面的 `got:id`**——这正是 offset 机制的作用。

复现命令：
```bash
docker run --rm --entrypoint sh -v "<repo>/scripts:/mnt/s:ro" \
  ghcr.io/fishcodetech/muteki-worker:latest -c 'python3 /mnt/s/test_mkt_session.py'
```

---

## 六、接入

| 位置 | 改动 |
|---|---|
| `muteki_runtime._seed_session_tool()` | 每次 Run 把 `scripts/mkt-session.py` 复制到 `<workspace>/muteki/mkt-session` |
| `known_exploit.SESSION_TOOL_GUIDE` | 780 字符，随每个 checklist 注入 `AGENTS.md` |

指南里明确写了**什么时候不该用**：「管道对单次请求/响应仍然是对的，只在交换多于一轮时才用会话」。否则会变成「每次交互起一个 daemon」，得不偿失。

### 一个路径坑
`parents[4]` 算错了（指向 `backend/`），正确是 `parents[5]`（repo 根）。已验证。

### checklist 体积变化
| 靶机 | 加 session 指南前 | 后 |
|---|---|---|
| Joomla | 2278 | 3061 |
| PHP | 3145 | 3928 |

裁剪比例从 0.48 变成 0.65。**这不是退化**——固定成本（session 指南 780）对所有靶机一视同仁，无关条目依然被裁掉了。测试断言相应改成「无关条目数减少 + 总量 <75%」而不是死守 0.6。

---

## 七、验证结果

| 项 | 结果 |
|---|---|
| 全量回归 | **2307 passed**（2300 → 2307） |
| 新增测试 | `test_checklist_trimming.py` +3（session 指南） |
| 端到端 | 容器内实测通过 |
| 后端 | 已重启 |
| 前端 | 200 |
| 镜像 | **未改动** |

---

## 八、遗留

- `--pty` 选项预留但未实现（大多数 CTF 服务是行导向的，pty 会把输入回显进日志，反而污染输出）。真需要时再加。
- 会话不会自动清理。`close` 或容器销毁时清掉；`list` 能看到残留的僵死会话。
- **未在真实靶机上验证过**。测试用的是本地 stub server。第一个真实用例可能会暴露协议假设问题（比如目标用 `\r` 而非 `\n` 作为行终止）。
