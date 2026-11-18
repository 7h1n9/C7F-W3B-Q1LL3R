"""Session-wide environment hygiene for the suite.

The suite talks to loopback targets: ephemeral HTTP servers it starts itself,
and the local test database.  Both ``httpx`` and ``urllib`` honour
``HTTP(S)_PROXY`` from the environment, and a test that *spawns* a subprocess
inherits it through ``os.environ.copy()``.

On an operator machine those variables point at a session-scoped egress proxy.
A loopback request then goes to that proxy instead of to the test server, and
hangs until its timeout.  Observed 2026-11-03:
``test_poc_bundle_script_replays_cookie_session_and_reports_digest`` failed on
its 15-second subprocess timeout with ``HTTP_PROXY=http://127.0.0.1:38261`` in
the shell, and passed in 0.7s once the variable was gone -- the same mechanism
that turned every arena call into ``ARENA_MCP_UNREACHABLE`` during an
unattended batch (see ``Settings.respect_proxy_env``).

Clearing them for the session makes a green run a property of the code under
test rather than of whichever terminal started pytest.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.fixture(scope="session", autouse=True)
def _no_ambient_proxy() -> Iterator[None]:
    """Drop inherited proxy variables, and put them back afterwards."""

    saved = {name: os.environ.pop(name) for name in _PROXY_VARIABLES if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
