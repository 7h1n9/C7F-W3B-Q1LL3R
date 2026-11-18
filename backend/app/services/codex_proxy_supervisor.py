"""Supervise the standalone Codex Responses proxy from the backend process.

与 :class:`app.services.claude_proxy_supervisor.ClaudeProxySupervisor` 同构，
理由逐条相同，这里只记结论：

* 代理是独立进程（必须绑 ``0.0.0.0`` 的随机端口，且 Worker 容器经宿主别名
  访问它——后端自己的 socket 两者都做不到）。在这里启动，运营者就不必多开
  一个终端；在这里监管，两者才不会失同步：
  **随后端启动**、**随后端停止**（含崩溃——子进程带 ``--parent-pid`` 看门狗，
  不会活过大进程并继续从一个没人看着的端口花模型额度）、**早死只重启一次**
  （绑定失败值得一次重试，崩溃循环不值得）。

刻意不做的事
------------

不去读回端口并缓存。状态文件是唯一真源，
:func:`app.core.config.codex_proxy_endpoint` 每次使用都重读（并做存活探测）。
在这里缓存会造出第二份可能过期的真源——子进程换端口重启时就对不上了。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 等子进程发布状态文件的时限。相对它要做的事（import + bind）很宽裕，
#: 因为冷缓存下 uvicorn 与 app 的首次 import 是慢的那部分。
_STARTUP_TIMEOUT_SECONDS = 25.0

#: 等状态文件出现并可用时的轮询间隔。
_POLL_INTERVAL_SECONDS = 0.25


class CodexProxySupervisor:
    """在后端生命周期内持有代理子进程。"""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """启动代理，除非它已经在跑。

        已在跑就放着不动而不是替换：那可能是运营者按文档手工启动的，杀掉它换
        一个一模一样的会把 token 从正在用它的 Worker 脚下抽掉。
        """

        settings = get_settings()
        if not settings.codex_proxy_autostart:
            logger.info("Codex proxy autostart is disabled; not starting it.")
            return

        from app.core.config import codex_proxy_endpoint

        if codex_proxy_endpoint() is not None:
            logger.info("Codex proxy is already listening; leaving it as is.")
            return

        async with self._lock:
            if self.running:
                return
            # 在锁内复查：两个调用者可能在任一方拿锁前都通过了上面那次无锁检查。
            if codex_proxy_endpoint() is not None:
                return
            await self._spawn(settings.codex_proxy_state_file)

    async def _spawn(self, state_file: str) -> None:
        backend_root = Path(__file__).resolve().parents[2]
        state_path = Path(state_file)
        # 上次被杀留下的文件会让下面的等待在一个死端口上「成功」，所以先清掉
        # ——子进程会重写它。
        state_path.unlink(missing_ok=True)

        command = [
            sys.executable,
            "-m",
            "app.codex_proxy_launcher",
            "--state-file",
            str(state_path),
            "--parent-pid",
            str(os.getpid()),
        ]
        logger.info("Starting Codex proxy: %s", " ".join(command))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(backend_root),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            # 不致命：codex 渠道只是回落到它存储的 base_url，即代理之前的行为。
            logger.warning("Could not start the Codex proxy: %s", error)
            return

        if await self._await_listening(state_path):
            logger.info("Codex proxy is up (pid %s).", self._process.pid)
        else:
            logger.warning(
                "Codex proxy did not publish a listening port within %.0fs.",
                _STARTUP_TIMEOUT_SECONDS,
            )

    async def _await_listening(self, state_path: Path) -> bool:
        """等到子进程的状态文件出现且端口有应答。"""

        from app.core.config import codex_proxy_endpoint

        deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                return False
            if state_path.exists() and codex_proxy_endpoint() is not None:
                return True
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        return False

    async def stop(self) -> None:
        """终止子进程并等它离开，然后清掉它的状态文件。

        先 ``terminate``：launcher 在 ``finally`` 里删状态文件，SIGKILL 会跳过
        那一步留下过期文件。kill 只是对付「无视礼貌请求」的子进程的兜底。

        之后无条件删一次状态文件，而不是留给子进程。子进程只在它执行到
        ``finally`` 时才清理——而整个进程树被一次性杀掉时，恰恰就是它执行不到
        的时候，也正是本方法要处理的情形。删一个子进程已经删过的文件是空操作，
        所以在这里做零成本，还覆盖了它做不到的情形。
        """

        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Codex proxy ignored terminate; killing it.")
                process.kill()
                await process.wait()
        self._unlink_state_file()

    @staticmethod
    def _unlink_state_file() -> None:
        """如果代理的状态文件还在，删掉它。"""

        raw = str(get_settings().codex_proxy_state_file or "").strip()
        if not raw:
            return
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError as error:
            logger.debug("Could not remove the Codex proxy state file: %s", error)


codex_proxy_supervisor = CodexProxySupervisor()