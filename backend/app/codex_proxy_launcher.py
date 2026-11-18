"""Standalone launcher for the Codex Responses proxy, on a random port.

与 :mod:`app.claude_proxy_launcher` 同构，理由也一样，这里只重述结论并点出
本代理特有的一处：

* **必须绑 0.0.0.0**：Worker 容器经 Docker 网关别名（``host.docker.internal``）
  访问宿主，只绑回环的代理在容器里不可见，每个 codex Worker 都会健康探针失败。
* **随机端口 + token**：绑到所有接口就必然要 token，否则局域网内任何人都能
  拿它花上游的模型额度。端口随机是为了不和后端（8000）/ Runner（8091）撞。
* **状态文件**：后端只能从「持有 socket 的那个进程写下的文件」得知端口。

本代理特有：codex CLI 的端点不是环境变量，而是平台注入的
``-c model_providers.muteki.base_url=...`` 参数（见
``muteki/solver/cli_driver.py::_codex_config_flags``）。所以这里的
``container_base_url`` 同样是「容器里该怎么写这个 base_url」的答案。

Usage::

    python -m app.codex_proxy_launcher                  # foreground
    python -m app.codex_proxy_launcher --parent-pid 123 # exit when 123 does
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

#: 与 claude 侧同样的理由：放在 backend 目录下随检出一起走，而不是系统临时目录
#: （后者可能在代理还活着时被清掉）。
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[2] / ".codex-proxy.json"


def free_port() -> int:
    """向 OS 要一个未占用端口。

    用「绑 0 再读回」是唯一可靠的方式：扫描一个「大概空闲」的号码会和同时
    启动的其它进程撞车。
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])


def write_state(path: Path, *, host: str, port: int, token: str) -> None:
    """记录端点供后端读取，且仅限本用户可读。

    文件带 token，所以要锁权限。Windows 上 ``os.chmod`` 只切只读位，ACL 仍然
    给 ``Users`` 读权限——那才是本平台真正的权限模型，所以这里显式收紧 ACL，
    ``chmod`` 保留给 POSIX。原子写入，读者看不到半截 JSON。
    """

    payload = {
        "host": host,
        "port": port,
        "token": token,
        # 给容器用的值：容器用不了宿主的回环。
        "container_base_url": f"http://host.docker.internal:{port}",
        "host_base_url": f"http://127.0.0.1:{port}",
        "pid": os.getpid(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _restrict_to_owner(tmp)
    tmp.replace(path)


def _restrict_to_owner(path: Path) -> None:
    """让 ``path`` 仅当前用户可读。"""

    if os.name == "nt":
        # icacls /inheritance:r 丢弃继承来的 ACE，再把属主授到完全控制。
        # 其余主体（Users、Authenticated Users）就都没有访问权了。
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=False,
        )
        return
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _watch_parent(parent_pid: int) -> None:
    """父进程没了就退出。

    后端会在正常关闭时终止这个子进程，但对后端的 SIGKILL——或一次崩溃——
    永远走不到那段代码。没有这个看门狗，代理会活下来，仍然绑在 ``0.0.0.0``、
    仍然攥着上游凭据。轮询父进程存活是个粗糙的信号，但对一个从没机会说再见
    的父进程来说，这是唯一可得的信号。
    """

    while True:
        time.sleep(2)
        if not _process_alive(parent_pid):
            # 用 ``os._exit`` 而非 ``sys.exit``：这段跑在非主线程，抛 SystemExit
            # 只会结束这个线程，服务器还活着。
            os._exit(0)


def _process_alive(pid: int) -> bool:
    """``pid`` 是否还存在，且不需要拥有它。"""

    if os.name == "nt":
        # Windows 没有 signal-0 探测；``tasklist`` 是可移植的替代。它的退出码
        # 无论找没找到都是 0，所以要看输出。
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在，只是不归我们发信号——那也是「活着」。
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Codex Responses proxy on a random port."
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=0, help="port; 0 picks a free one")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument(
        "--token",
        default="",
        help="shared secret; generated when omitted (pass an empty value to disable)",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="exit when this process disappears; 0 disables the watchdog",
    )
    args = parser.parse_args(argv)

    port = args.port or free_port()
    token = args.token or secrets.token_urlsafe(32)

    # 代理从 settings 读 token，所以要在 import app.core.config 之前进环境。
    os.environ["APP_CODEX_PROXY_TOKEN"] = token
    os.environ.setdefault("APP_CODEX_PROXY_STATE_FILE", args.state_file)

    state_path = Path(args.state_file)
    write_state(state_path, host=args.host, port=port, token=token)

    if args.parent_pid:
        threading.Thread(
            target=_watch_parent, args=(args.parent_pid,), name="parent-watchdog", daemon=True
        ).start()

    import uvicorn

    from app.codex_proxy_app import app

    print(f"Codex proxy listening on {args.host}:{port}", file=sys.stderr)
    print(f"  containers : http://host.docker.internal:{port}", file=sys.stderr)
    print(f"  state file : {state_path}", file=sys.stderr)
    try:
        uvicorn.run(app, host=args.host, port=port, log_level="info")
    finally:
        # 过期的状态文件会把后端指向一个死端口，看起来像上游故障，所以随进程一起
        # 消失。只在它还是我们的才删：重启后的代理可能已经写了自己的。
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if not isinstance(current, dict) or current.get("pid") == os.getpid():
            state_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())