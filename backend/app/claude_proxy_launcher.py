"""Standalone launcher for the Claude proxy, on a random port.

Why a launcher at all
---------------------
The proxy has to be reachable *from the Worker container*, not just from this
host. A container reaches the host through the Docker gateway alias
(``host.docker.internal``), which only works if the proxy binds ``0.0.0.0`` -- a
loopback-only bind is invisible from inside the container and every Claude Worker
would fail its health probe.

Binding ``0.0.0.0`` is exactly what makes a random port and a token necessary:

* **Random port.** The backend already owns 8000 and the Runner owns 8091. Picking
  a free port means the proxy cannot collide with either, and a port nobody
  guessed is a smaller target than one published in a config file.
* **Token.** Once bound to all interfaces, anything on the LAN can post to it and
  spend the upstream model credentials. The launcher generates a secret and the
  proxy demands it; the value is written to a ``0600`` state file rather than
  logged, because a secret in a log is a leaked secret.

The state file is how the backend learns the port. Discovering a random port any
other way (parsing logs, scanning) is fragile; a file written by the process that
owns the socket is not.

The app it serves
-----------------
:mod:`app.claude_proxy_app`, *not* ``app.main``. The backend auto-starts this
launcher as a child process, so importing ``app.main`` here would make every
child enter the same lifespan and start a grandchild -- an unbounded process
tree. See that module for the full reasoning.

Usage::

    python -m app.claude_proxy_launcher                  # foreground
    python -m app.claude_proxy_launcher --parent-pid 123 # exit when 123 does
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

#: Written where the backend looks by default.  Kept under the backend directory
#: so it travels with the checkout rather than into a system temp dir that may be
#: wiped between runs while the proxy is still up.
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[2] / ".claude-proxy.json"


def free_port() -> int:
    """Ask the OS for an unused port.

    Binding with port 0 and reading back the assignment is the only reliable way:
    scanning for a "probably free" number races with whatever else is starting at
    the same moment.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])


def write_state(path: Path, *, host: str, port: int, token: str) -> None:
    """Record the endpoint for the backend, readable only by this user.

    The file carries the token, so it is locked down. ``os.chmod`` is not enough
    on Windows -- it only toggles the read-only bit and leaves the ACL granting
    ``Users`` read access, which is the platform's real permission model. So the
    ACL is tightened explicitly there and ``chmod`` is kept for POSIX.

    Written atomically so a reader never sees a half-written JSON document.
    """

    payload = {
        "host": host,
        "port": port,
        "token": token,
        # The value to hand a container, which cannot use the host's loopback.
        "container_base_url": f"http://host.docker.internal:{port}",
        "host_base_url": f"http://127.0.0.1:{port}",
        "pid": os.getpid(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _restrict_to_owner(tmp)
    tmp.replace(path)


def _restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable only by the current user."""

    if os.name == "nt":
        # icacls with /inheritance:r drops inherited ACEs, then the owner is
        # granted full control. Anything else (Users, Authenticated Users) is
        # left without access.
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
    """Exit when the parent process is gone.

    The backend terminates this child on a clean shutdown, but a SIGKILL on the
    backend -- or a crash -- never reaches that code. Without this watchdog the
    proxy would survive, still bound to ``0.0.0.0`` and still holding the upstream
    credentials. Polling the parent's liveness is a crude signal but it is the
    only one available for a parent that was never given a chance to say goodbye.
    """

    while True:
        time.sleep(2)
        if not _process_alive(parent_pid):
            # ``os._exit`` rather than ``sys.exit``: this runs on a non-main
            # thread, where raising SystemExit would only end this thread and
            # leave the server running.
            os._exit(0)


def _process_alive(pid: int) -> bool:
    """Whether ``pid`` still exists, without needing to own it."""

    if os.name == "nt":
        # On Windows there is no signal-0 probe; ``tasklist`` is the portable way
        # to ask. Its exit code is 0 whether or not the pid was found, so the
        # output has to be inspected.
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
        # Exists, just not ours to signal -- which still means "alive".
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Claude proxy on a random port.")
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

    # The proxy reads the token from settings, so it has to be in the environment
    # before app.core.config is imported.
    os.environ["APP_CLAUDE_PROXY_TOKEN"] = token
    os.environ.setdefault("APP_CLAUDE_PROXY_STATE_FILE", args.state_file)

    state_path = Path(args.state_file)
    write_state(state_path, host=args.host, port=port, token=token)

    if args.parent_pid:
        threading.Thread(
            target=_watch_parent, args=(args.parent_pid,), name="parent-watchdog", daemon=True
        ).start()

    import uvicorn

    from app.claude_proxy_app import app

    print(f"Claude proxy listening on {args.host}:{port}", file=sys.stderr)
    print(f"  containers : http://host.docker.internal:{port}", file=sys.stderr)
    print(f"  state file : {state_path}", file=sys.stderr)
    try:
        uvicorn.run(app, host=args.host, port=port, log_level="info")
    finally:
        # A stale state file would point the backend at a dead port and look like
        # an upstream outage, so it goes away with the process. Only remove it if
        # it is still ours: a restarted proxy may already have written its own.
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if not isinstance(current, dict) or current.get("pid") == os.getpid():
            state_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
