"""End-to-end test for mkt-session: does a conversation actually work?

Run inside the worker image:

    docker run --rm --entrypoint sh \
      -v "<repo>/scripts:/mnt/s:ro" <image> /mnt/s/test_mkt_session.sh

The stub server below speaks first (a banner and a prompt), then answers each
line it receives.  That is the shape a pipe cannot handle: with
``printf | nc`` the banner and the reply are indistinguishable, and the call
returns only when the server closes.  ``mkt-session`` must return the banner on
``start`` and each reply on its own ``send``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PORT = 9998


def serve(stop: threading.Event) -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", PORT))
    server.listen(1)
    server.settimeout(15)
    try:
        conn, _ = server.accept()
    except OSError:
        return
    conn.sendall(b"WELCOME\r\nlogin: ")
    buffer = b""
    while not stop.is_set():
        try:
            data = conn.recv(4096)
        except OSError:
            break
        if not data:
            break
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            conn.sendall(b"got:" + line.strip() + b"\r\nnext: ")
    conn.close()
    server.close()


def main() -> int:
    tool = str(Path(__file__).resolve().parent / "mkt-session.py")
    env = dict(os.environ, MKT_SESSION_DIR="/tmp/mkt-session-test")
    stop = threading.Event()
    thread = threading.Thread(target=serve, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.4)

    def run(*args: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, tool, *args], capture_output=True, text=True, env=env, timeout=30
        )
        return proc.returncode, proc.stdout, proc.stderr

    failures: list[str] = []

    code, out, err = run("start", "--host", "127.0.0.1", "--port", str(PORT), "--name", "e2e", "--wait", "1.2")
    print("--- start ---")
    print(out.rstrip() or "(no stdout)")
    if code != 0:
        failures.append(f"start rc={code}: {err.strip()}")
    if "WELCOME" not in out:
        failures.append("start did not return the banner")

    code, out, err = run("send", "--name", "e2e", "--data", "id", "--wait", "1.0")
    print("--- send id ---")
    print(out.rstrip() or "(no stdout)")
    if "got:id" not in out:
        failures.append(f"send id did not echo the reply: {out!r} {err.strip()}")

    code, out, err = run("send", "--name", "e2e", "--data", "whoami", "--wait", "1.0")
    print("--- send whoami ---")
    print(out.rstrip() or "(no stdout)")
    if "got:whoami" not in out:
        failures.append(f"send whoami did not echo the reply: {out!r} {err.strip()}")
    # The second reply must not re-print the first one: that is the whole point
    # of tracking an offset instead of dumping the log each time.
    if "got:id" in out:
        failures.append("send replayed earlier output")

    code, out, _ = run("list")
    print("--- list ---")
    print(out.rstrip())
    if "open" not in out:
        failures.append("list did not report an open session")

    code, out, _ = run("close", "--name", "e2e")
    print("--- close ---")
    print(out.rstrip())
    stop.set()

    if failures:
        print("\nFAIL:")
        for item in failures:
            print("  -", item)
        return 1
    print("\nOK: banner on start, per-command replies, no replay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
