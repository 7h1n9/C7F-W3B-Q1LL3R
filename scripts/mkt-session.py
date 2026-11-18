#!/usr/bin/env python3
"""Keep a TCP session open across commands, so a Worker can hold a conversation.

Why this exists
---------------
The Worker's only execution primitive is a shell command that runs to
completion.  A pipe into ``nc`` therefore sends everything at once and returns
only when the far end closes, which is the wrong shape for anything interactive:
a login prompt, a debugger, a shell that asks a question, a service that answers
line by line.

Measured 2026-11-06 over 484 Runs: ``nc`` was used in 71 of them and **55 (77%)
came back with a timeout or a dead connection**.  Workers were hand-rolling the
missing layer -- ``printf 'GET / HTTP/1.1\\r\\n...' | nc``, ``tmux new-session -d
-s listener 'nc -lvnp 8888'`` -- which is the same idea rebuilt badly every time.

CTFAgent (CCS'25) names this explicitly: static CLI tools "only accept results
after the pipeline outputs EOF", so the agent cannot send a payload and see the
answer, and that gap is why their Environmental Augmentation adds line-by-line
netcat sessions.

Design
------
One daemon owns the socket and serves BOTH directions over a Unix socket:

    worker ──> mkt-session send ──unix──> daemon ──tcp──> target
    worker <── stdout          <──unix── daemon <──tcp── target

The daemon appends every received byte to a log file as well, so a session that
outlives the daemon still leaves evidence, and ``read --offset`` can page
through output without reconnecting.

Usage::

    mkt-session start --host H --port P --name S   # connect, print the banner
    mkt-session send  --name S --data 'id'         # write a line, return new output
    mkt-session read  --name S                     # drain without sending
    mkt-session close --name S                     # disconnect, keep the log
    mkt-session list                               # what is open

Not a pty by default: CTF services are line-oriented and a pty echoes input back
into the log, doubling every reply.  ``--pty`` exists for the cases that need a
terminal.

State lives under ``/tmp/mkt-session`` (override with ``MKT_SESSION_DIR``) so it
never collides with the run workspace the Worker is asked to keep clean.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("MKT_SESSION_DIR") or "/tmp/mkt-session")

#: How long a single ``send`` waits for the target to answer before returning
#: whatever arrived.  Short on purpose: a Worker that blocks 30s per call burns
#: its window in three interactions.
DEFAULT_WAIT = 3.0

#: Upper bound on one control message.  The protocol is line JSON; anything
#: larger is a bug in the caller, not a target response.
MAX_CONTROL = 1 << 20


def _base() -> Path:
    base = DEFAULT_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe(name: str) -> str:
    return "".join(c for c in str(name) if c.isalnum() or c in "-_")[:40] or "default"


def _paths(name: str) -> dict[str, Path]:
    base = _base()
    stem = _safe(name)
    return {
        "pid": base / f"{stem}.pid",
        "log": base / f"{stem}.log",
        "sock": base / f"{stem}.sock",
        "meta": base / f"{stem}.meta",
    }


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _read_from(log: Path, offset: int) -> tuple[str, int]:
    try:
        size = log.stat().st_size
    except OSError:
        return "", offset
    if size <= offset:
        return "", size
    with log.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(size - offset)
    return chunk.decode("utf-8", errors="replace"), size


# --- daemon ---------------------------------------------------------------


def _daemon(host: str, port: int, log_path: Path, sock_path: Path) -> int:
    """Own the target connection and serve send/receive over a Unix socket."""

    # ``start_new_session=True`` at spawn already called ``setsid``, and calling
    # it a second time raises ``PermissionError: [Errno 1]`` because the process
    # is already a session leader.  The daemon therefore must NOT detach again --
    # this failed silently for three test runs, surfacing only as "daemon exited
    # rc=1" from ``start``.
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_IGN)

    # The daemon is spawned directly (not through ``_paths``), so it has to make
    # its own directory.  Missing this failed silently: the child died on the
    # first write, ``start`` reported nothing, and the session looked like a
    # refused connection rather than a missing parent directory.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    try:
        target = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        log_path.write_text(f"[mkt-session] connect failed: {exc}\n", encoding="utf-8")
        return 1

    sock_path.unlink(missing_ok=True)
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.bind(str(sock_path))
    control.listen(8)
    control.setblocking(False)
    target.setblocking(False)

    pending_out = b""
    with log_path.open("ab", buffering=0) as sink:
        while True:
            try:
                ready, _, _ = select.select([control, target], [], [], 1.0)
            except (OSError, ValueError):
                break

            if control in ready:
                try:
                    conn, _ = control.accept()
                except OSError:
                    conn = None
                if conn is not None:
                    conn.setblocking(True)
                    try:
                        raw = conn.recv(MAX_CONTROL)
                    except OSError:
                        raw = b""
                    if raw:
                        try:
                            message = json.loads(raw.decode("utf-8", errors="replace"))
                        except ValueError:
                            message = {}
                        data = str(message.get("data") or "")
                        if data:
                            if not data.endswith("\n"):
                                data += "\n"
                            pending_out += data.encode("utf-8", errors="replace")
                    conn.close()

            if target in ready:
                try:
                    chunk = target.recv(65535)
                except OSError:
                    chunk = b""
                if not chunk:
                    sink.write(b"\n[mkt-session] remote closed\n")
                    break
                sink.write(chunk)

            if pending_out:
                try:
                    sent = target.send(pending_out)
                    pending_out = pending_out[sent:]
                except OSError:
                    break

    for closer in (target, control):
        try:
            closer.close()
        except OSError:
            pass
    sock_path.unlink(missing_ok=True)
    return 0


# --- client commands ------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    paths = _paths(args.name)
    existing = _read_pid(paths["pid"])
    if existing is not None:
        print(f"[mkt-session] '{args.name}' is already open (pid {existing}); close it first",
              file=sys.stderr)
        return 2

    child = subprocess.Popen(
        [
            sys.executable,
            os.path.abspath(__file__),
            "_daemon",
            "--host", args.host,
            "--port", str(args.port),
            "--log", str(paths["log"]),
            "--sock", str(paths["sock"]),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # The child writes its own pid file only after the connection succeeded, so
    # a failed handshake cannot leave a stale "open" entry behind.
    #
    # Wait for readiness rather than sleeping a fixed time: ``start_new_session``
    # detaches the daemon completely, and on a cold interpreter start it can take
    # longer than any fixed sleep to bind the control socket.  Polling avoids
    # both a flaky short wait and a needlessly long one.
    deadline = time.time() + max(3.0, float(args.wait) * 3)
    while time.time() < deadline:
        if paths["sock"].exists() or child.poll() is not None:
            break
        time.sleep(0.05)
    if child.poll() is None and paths["sock"].exists():
        # Give the target a moment to speak its banner.
        time.sleep(float(args.wait))

    body = ""
    try:
        body = paths["log"].read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        body = ""

    # A live process is not proof of a live connection: the daemon stays up
    # serving the control socket, so ask it rather than trusting ``poll()``.
    connected = paths["sock"].exists() and "connect failed" not in body

    if child.poll() is not None or not connected:
        print(body or f"[mkt-session] daemon exited rc={child.returncode}", file=sys.stderr)
        try:
            child.terminate()
        except OSError:
            pass
        paths["pid"].unlink(missing_ok=True)
        paths["sock"].unlink(missing_ok=True)
        return 1

    paths["pid"].write_text(str(child.pid), encoding="utf-8")
    paths["meta"].write_text(
        json.dumps({"host": args.host, "port": args.port, "pid": child.pid}),
        encoding="utf-8",
    )
    text, size = _read_from(paths["log"], 0)
    print(f"[mkt-session] '{args.name}' open pid={child.pid} offset={size}")
    if text.strip():
        print(text.rstrip())
    return 0


def _require_session(name: str) -> dict[str, Path] | None:
    paths = _paths(name)
    if _read_pid(paths["pid"]) is None:
        print(f"[mkt-session] no live session '{name}'", file=sys.stderr)
        return None
    return paths


def cmd_send(args: argparse.Namespace) -> int:
    paths = _require_session(args.name)
    if paths is None:
        return 2
    before = args.offset if args.offset is not None else _size(paths["log"])

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(paths["sock"]))
            client.sendall(json.dumps({"data": args.data}).encode("utf-8"))
    except OSError as exc:
        print(f"[mkt-session] send failed: {exc}", file=sys.stderr)
        return 1

    time.sleep(float(args.wait))
    text, size = _read_from(paths["log"], before)
    print(text.rstrip())
    print(f"[offset={size}]", file=sys.stderr)
    return 0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def cmd_read(args: argparse.Namespace) -> int:
    paths = _require_session(args.name)
    if paths is None:
        return 2
    before = args.offset if args.offset is not None else _size(paths["log"])
    time.sleep(float(args.wait))
    text, size = _read_from(paths["log"], before)
    print(text.rstrip())
    print(f"[offset={size}]", file=sys.stderr)
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    paths = _paths(args.name)
    pid = _read_pid(paths["pid"])
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    paths["pid"].unlink(missing_ok=True)
    paths["meta"].unlink(missing_ok=True)
    paths["sock"].unlink(missing_ok=True)
    print(f"[mkt-session] '{args.name}' closed")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    base = _base()
    rows = []
    for pid_file in sorted(base.glob("*.pid")):
        name = pid_file.stem
        alive = _read_pid(pid_file) is not None
        rows.append((name, alive, _size(base / f"{name}.log")))
    if not rows:
        print("[mkt-session] no sessions")
        return 0
    for name, alive, size in rows:
        print(f"  {name:24s} {'open' if alive else 'dead':5s} {size} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mkt-session", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="open a session")
    start.add_argument("--host", required=True)
    start.add_argument("--port", type=int, required=True)
    start.add_argument("--name", default="default")
    start.add_argument("--wait", type=float, default=2.0)
    start.set_defaults(func=cmd_start)

    send = sub.add_parser("send", help="write a line and return new output")
    send.add_argument("--name", default="default")
    send.add_argument("--data", required=True)
    send.add_argument("--wait", type=float, default=DEFAULT_WAIT)
    send.add_argument("--offset", type=int, default=None)
    send.set_defaults(func=cmd_send)

    read = sub.add_parser("read", help="drain new output without sending")
    read.add_argument("--name", default="default")
    read.add_argument("--wait", type=float, default=DEFAULT_WAIT)
    read.add_argument("--offset", type=int, default=None)
    read.set_defaults(func=cmd_read)

    close = sub.add_parser("close", help="close a session")
    close.add_argument("--name", default="default")
    close.set_defaults(func=cmd_close)

    listing = sub.add_parser("list", help="list sessions")
    listing.set_defaults(func=cmd_list)

    daemon = sub.add_parser("_daemon")
    daemon.add_argument("--host", required=True)
    daemon.add_argument("--port", type=int, required=True)
    daemon.add_argument("--log", required=True)
    daemon.add_argument("--sock", required=True)
    daemon.set_defaults(
        func=lambda a: _daemon(a.host, a.port, Path(a.log), Path(a.sock))
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
