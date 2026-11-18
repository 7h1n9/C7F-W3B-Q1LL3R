"""The benchmark range: where it lives, and how to bring it back up.

Two concerns share this module because they are two halves of one thing:

* **Where it is** — a case records where the challenge *would* live, not where
  it lives right now.  The range's host port is re-chosen per repetition so an
  agent cannot memorise an address and win by recall instead of by reasoning
  (CAP-34, the runtime-augmentation finding in CTF-Dojo).  Case targets
  therefore carry ``{{host}}`` / ``{{port}}`` placeholders resolved here.
* **How to bring it back up** — picking a free port, writing it into the range's
  ``.env``, recreating the web container, and re-seeding the flag through the
  range's own idempotent seed script.

The placeholder syntax follows the convention already used elsewhere in this
codebase for values that must not be baked into recorded content
(``SensitiveValueSanitizer`` writes ``{{target_url}}`` / ``{{target_host}}`` /
``{{flag_pattern}}`` into stored write-ups), so nothing new has to be learned to
read these files.

All subprocess work is confined to :func:`bring_up`: importing this module never
touches Docker, so the API process and the test suite can resolve a target
without a range running.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# The range publishes its web port on the host loopback interface.  These
# defaults mirror ``ranges/asset-warranty-mysql/.env``.
DEFAULT_RANGE_HOST = "127.0.0.1"
DEFAULT_RANGE_PORT = 28037

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RANGE_DIR = PROJECT_ROOT / "ranges" / "asset-warranty-mysql"

# The only range this repo ships.  A case that names a different one is skipped
# rather than executed against the wrong target.
DEFAULT_RANGE_NAME = "asset-warranty-mysql"

# Service and file names owned by the range's own compose file.  Kept here so
# the randomiser cannot drift away from what compose actually declares.
WEB_SERVICE = "asset-warranty-web"
DB_SERVICE = "asset-warranty-db"
SEED_SCRIPT = "/docker-entrypoint-initdb.d/002-seed.sh"
PORT_ENV_KEY = "WARRANTY_WEB_PORT"
FLAG_ENV_KEY = "WARRANTY_FLAG"

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_ENV_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


@dataclass(frozen=True, slots=True)
class RangeEndpoint:
    """Where the range is reachable from the process executing the solver."""

    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def read_env_values(env_path: Path) -> dict[str, str]:
    """Read simple ``KEY=value`` pairs out of a ``.env`` file."""

    path = Path(env_path)
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE.match(line)
        if match:
            values.setdefault(match.group(2), match.group(4).strip())
    return values


def range_endpoint() -> RangeEndpoint:
    """Read the live range endpoint.

    Precedence is environment, then the range's own ``.env``, then the built-in
    default.  The ``.env`` step matters: compose resolves the published port
    from that file, so it — not a constant — is the source of truth, and a
    randomised run only has to rewrite it for resolution to follow along.
    """

    host = (os.getenv("C7F_RANGE_HOST") or DEFAULT_RANGE_HOST).strip()
    if not host:
        raise ValueError("C7F_RANGE_HOST must not be empty")

    raw_port = os.getenv("C7F_RANGE_PORT")
    if raw_port is None:
        raw_port = read_env_values(DEFAULT_RANGE_DIR / ".env").get(PORT_ENV_KEY)
    if raw_port is None:
        raw_port = str(DEFAULT_RANGE_PORT)

    raw_port = raw_port.strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(
            f"range port must be an integer; got {raw_port!r} "
            f"(from C7F_RANGE_PORT or {PORT_ENV_KEY} in the range .env)"
        ) from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"range port must be in 1..65535; got {port}")

    return RangeEndpoint(host=host, port=port)


def resolve_case_target(target: str, endpoint: RangeEndpoint | None = None) -> str:
    """Substitute ``{{host}}`` / ``{{port}}`` into a case target.

    An unknown placeholder is an error rather than a silent pass-through: a typo
    that survived into a Run would send the agent at a literal ``{{host}}`` and
    waste the whole attempt.
    """

    resolved = endpoint if endpoint is not None else range_endpoint()
    values = {"host": resolved.host, "port": str(resolved.port)}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(
                f"unknown range placeholder {{{{{key}}}}} in case target {target!r}"
            )
        return values[key]

    return _PLACEHOLDER.sub(_replace, target)


def generate_flag() -> str:
    """A fresh range flag in the same shape the range seeds today."""

    return f"flag{{{secrets.token_hex(16)}}}"


def available_range() -> str:
    """The range this machine can actually serve."""

    return (os.getenv("C7F_RANGE_NAME") or DEFAULT_RANGE_NAME).strip() or DEFAULT_RANGE_NAME


def pick_free_port(*, host: str = DEFAULT_RANGE_HOST) -> int:
    """Reserve an ephemeral port and return it.

    ``ponytail:`` there is an inherent race between closing this socket and
    Docker binding the port, and the window is milliseconds; a collision
    surfaces as a failed health check rather than as a silently wrong result.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def write_env_values(env_path: Path, updates: dict[str, str]) -> None:
    """Update keys in a ``.env`` file in place, preserving everything else."""

    path = Path(env_path)
    lines: list[str] = []
    replaced: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _ENV_LINE.match(line)
            if match and match.group(2) in updates:
                key = match.group(2)
                replaced.add(key)
                lines.append(f"{match.group(1)}{key}={updates[key]}")
                continue
            lines.append(line)

    lines.extend(f"{key}={value}" for key, value in updates.items() if key not in replaced)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _compose(range_dir: Path, *args: str) -> None:
    completed = subprocess.run(
        ["docker", "compose", *args],
        cwd=str(range_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()[:600]
        raise RuntimeError(
            f"docker compose {' '.join(args)} failed "
            f"(exit {completed.returncode}): {output}"
        )


def wait_for_health(
    endpoint: RangeEndpoint,
    *,
    timeout_seconds: float = 90.0,
    interval_seconds: float = 2.0,
) -> bool:
    """Poll the range's own health endpoint until it reports ready."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{endpoint.base_url}/health", timeout=3
            ) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval_seconds)
    return False


def bring_up(
    *,
    range_dir: Path | None = None,
    port: int | None = None,
    flag: str | None = None,
    timeout_seconds: float = 90.0,
) -> tuple[RangeEndpoint, str]:
    """Re-randomise the range's host port and flag, and bring it back up.

    This is both the per-repetition reset (CAP-03) and the runtime augmentation
    (CAP-34): the lab is read-only, so there is no residual state to clear and
    the only thing a repetition must not inherit is the previous address and
    flag.  Pass ``port`` / ``flag`` to pin either value for debugging.

    The flag is re-seeded by re-running the range's own idempotent init script
    (``002-seed.sh`` uses ``ON DUPLICATE KEY UPDATE``), so no data volume is
    destroyed and a repetition costs a container restart rather than a full
    re-initialisation.

    Returns the endpoint to target and the flag in force.  The caller owns the
    flag: it is a scoring secret and must not be written into recorded artefacts.
    """

    directory = Path(range_dir or DEFAULT_RANGE_DIR)
    env_path = directory / ".env"
    if not env_path.is_file():
        raise FileNotFoundError(f"range env file not found: {env_path}")

    new_port = port or pick_free_port()
    new_flag = flag or generate_flag()
    write_env_values(env_path, {PORT_ENV_KEY: str(new_port), FLAG_ENV_KEY: new_flag})

    _compose(directory, "up", "-d", "--force-recreate", WEB_SERVICE)
    # The db container keeps the flag it was created with, so re-seed explicitly
    # with the new value instead of recreating it — recreating would rebuild the
    # database from scratch on every repetition.
    _compose(
        directory,
        "exec",
        "-T",
        "-e",
        f"{FLAG_ENV_KEY}={new_flag}",
        DB_SERVICE,
        "bash",
        SEED_SCRIPT,
    )

    endpoint = RangeEndpoint(host=DEFAULT_RANGE_HOST, port=new_port)
    if not wait_for_health(endpoint, timeout_seconds=timeout_seconds):
        raise RuntimeError(
            f"range did not become healthy at {endpoint.base_url}/health "
            f"within {timeout_seconds:.0f}s"
        )
    return endpoint, new_flag
