"""Shelled-CLI worker drivers — claude / codex as full agentic executors.

Why: the local DeepSeek code-driven kernel (one run_python tool-call per step)
lacks the execute→observe→refine depth to actually land an exploit. EXP-AB proved
a shelled `claude -p` solves challenges the code-driven swarm misses, and its flag
still passes the real provenance gate. So we delegate a focused intent to a CLI
agent that runs its OWN shell loop, and gate its output exactly as before.

Each driver is a thin per-CLI adapter: it builds the argv + manages a session id so
the single conclude-fallback turn (on a timeout) can resume the SAME session — there
is no multi-turn resume loop; a worker runs one execute pass and is then discarded.
We run bare-host against the
SUBSCRIPTION CLIs (full-strength model — the reason it solves). codex is included
but may be usage-limited; the swarm degrades to claude-only when a driver's
healthcheck fails.

This module is pure (builds argv + parses output); the solver runs the subprocess.
"""

from __future__ import annotations

import abc
import contextlib as _contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from muteki.core.cost import _DEFAULT_PRICE, CODEX_CACHED_INPUT_PER_M, PRICES
from muteki.solver.worker_profiles import base_engine_for_profile, profile_uses_endpoint

# ── engine binary resolution ─────────────────────────────────────────────────
# A worker shells `subprocess.run(["claude", ...])`, which resolves the FIRST
# `claude` on PATH. On this host (and easily on others) that can be a BROKEN
# third-party repackage — e.g. `@cometix/claude-code`, a Node "restored" build
# that crashes at parse time (`SyntaxError: Unexpected identifier`) under an
# older Node, never reaching the CLI. A worker pointed at it dies before it can
# solve, and the healthcheck just sees a non-zero exit and silently degrades the
# swarm. So we DON'T trust bare PATH order: resolve each engine to a real,
# runnable OFFICIAL binary and pin it.
#
# Precedence:
#   1. explicit override  — env MUTEKI_CLAUDE_BIN / MUTEKI_CODEX_BIN (operator wins)
#   2. known official install locations, in order
#   3. every `name` on PATH, skipping ones whose realpath looks like a known
#      bad repackage (cometix), taking the first that actually runs
#   4. bare `name` as a last resort (preserves old behavior if nothing else found)
_ENV_OVERRIDE = {
    "claude": "MUTEKI_CLAUDE_BIN",
    "codex": "MUTEKI_CODEX_BIN",
    "cursor": "MUTEKI_CURSOR_BIN",
}

# The on-disk binary basename for an engine, when it differs from the engine
# `name` we use everywhere else. Cursor's headless CLI ships as `cursor-agent`
# (the bare `cursor` launcher opens the GUI / is a different tool), so a PATH
# scan for the engine "cursor" must actually look for `cursor-agent`.
_BIN_NAME = {"cursor": "cursor-agent"}

# Official / first-party install locations we trust, highest first. `~` and
# `%VAR%` are expanded at resolve time. The local native installer and Homebrew
# cask are the two blessed macOS paths; /usr/local/bin covers a plain npm global
# on Linux.
#
# Cursor's Windows-native installer (`irm 'https://cursor.com/install?win32=true'`)
# is deliberately listed: it drops a `cursor-agent.cmd`/`.ps1` shim into
# %LOCALAPPDATA%\cursor-agent and appends that directory to the *user* PATH. A
# process already running when the install happened never sees the new PATH, and
# a .cmd shim is not an .exe, so a bare PATH scan misses it. Naming the location
# here makes resolution independent of when the installer ran.
_KNOWN_GOOD = {
    "claude": [
        "~/.local/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ],
    "codex": [
        "~/.local/bin/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ],
    "cursor": [
        "~/.local/bin/cursor-agent",
        "/opt/homebrew/bin/cursor-agent",
        "/usr/local/bin/cursor-agent",
        # Windows native installer (see note above).
        "%LOCALAPPDATA%/cursor-agent/cursor-agent.cmd",
    ],
}

# realpath substrings that mark a KNOWN-BAD repackage we must never select.
_BAD_REALPATH_MARKERS = ("@cometix", "cometix")

# Optional knowledge-base MCP. Muteki can let a worker query a KB MCP (your own
# security-intel / CVE / writeup index) as a first-class tool. There is no bundled
# KB service — set MUTEKI_KB_MCP_NAME to the server key from your .mcp.json (and
# enable kb on the run) to use one. Empty (the default) means "no KB", so the
# whole KB path is inert out of the box.
KB_MCP_NAME = os.environ.get("MUTEKI_KB_MCP_NAME", "").strip()


def _looks_bad(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    low = real.lower()
    return any(m in low for m in _BAD_REALPATH_MARKERS)


def _runs_ok(path: str) -> bool:
    """Does this binary actually execute (vs crash at load like the cometix build)?
    `--version` is the cheapest probe that distinguishes a real CLI from a binary
    that dies before parsing argv."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def resolve_engine_bin(name: str) -> str:
    """Resolve an engine name to a pinned, runnable binary path (see precedence
    above). Falls back to the bare name so callers always get *something*."""
    # 1. operator override — trusted as-is (don't second-guess an explicit path)
    env = _ENV_OVERRIDE.get(name)
    if env and os.environ.get(env):
        return os.path.expanduser(os.environ[env])

    # 2. known-good install locations
    for cand in _KNOWN_GOOD.get(name, []):
        # expandvars first: Windows entries are written as %LOCALAPPDATA%/...,
        # and expanduser alone leaves %VAR% untouched.
        p = os.path.expanduser(os.path.expandvars(cand))
        if Path(p).exists() and not _looks_bad(p) and _runs_ok(p):
            return p

    # 3. PATH scan, skipping known-bad repackages, first that runs wins. The
    #    on-disk basename may differ from the engine name (cursor → cursor-agent).
    bin_basename = _BIN_NAME.get(name, name)
    for p in _which_all(bin_basename):
        if not _looks_bad(p) and _runs_ok(p):
            return p

    # 4. last resort — bare basename (old behavior). If everything is broken we
    #    at least fail the same way we used to, not worse.
    return bin_basename


def resolve_engine_bin_source(name: str) -> str:
    """Where would resolve_engine_bin() get this engine's binary from?

    Returns one of: "env" (explicit MUTEKI_*_BIN override), "known-good" (a
    blessed install location), "path" (a PATH scan hit), or "fallback" (nothing
    found — bare name). Drives the FE's "you're on an unpinned default path,
    consider setting MUTEKI_<ENGINE>_BIN" guidance for local mode.
    """
    env = _ENV_OVERRIDE.get(name)
    if env and os.environ.get(env):
        return "env"
    for cand in _KNOWN_GOOD.get(name, []):
        p = os.path.expanduser(os.path.expandvars(cand))
        if Path(p).exists() and not _looks_bad(p) and _runs_ok(p):
            return "known-good"
    bin_basename = _BIN_NAME.get(name, name)
    for p in _which_all(bin_basename):
        if not _looks_bad(p) and _runs_ok(p):
            return "path"
    return "fallback"


def _which_all(name: str) -> list[str]:
    """Every `name` found on PATH, in PATH order (shutil.which only returns one)."""
    out: list[str] = []
    seen: set[str] = set()
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, name)
        if cand not in seen and os.path.isfile(cand) and os.access(cand, os.X_OK):
            seen.add(cand)
            out.append(cand)
    # also let shutil.which have a say (handles PATHEXT etc.) as a backstop
    w = shutil.which(name)
    if w and w not in seen:
        out.append(w)
    return out


@dataclass
class CliResult:
    """One CLI run's outcome, normalized across engines."""
    text: str                       # the agent's final response / transcript tail
    session: Optional[str] = None   # session id, for a resume/conclude turn
    cost_usd: Optional[float] = None
    # token usage for this run, when the engine reports it. None == not reported.
    # claude exposes it via the result `usage` block; codex via turn.completed
    # `usage`. Fed to the cost ledger so the deck can show a token-usage column
    # alongside the $ figure (and so codex — which no longer reports a dollar
    # cost — still gets priced from its tokens). cursor reports neither.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # Cache buckets are reported separately by both vendors (claude:
    # cache_read_input_tokens / cache_creation_input_tokens, cursor:
    # cacheReadTokens / cacheWriteTokens).  Kept apart from input_tokens so a
    # billed-input view can exclude cache hits, which price differently.
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    # True when the token counts were inferred (streamed estimate / chars-per-token
    # floor) rather than reported by the vendor.  The cost ledger must not present
    # an inferred number as a metered one.
    usage_estimated: bool = False
    cost_estimated: bool = False
    # Stable identity for one usage record, so a caller that observes the same
    # turn twice (final parse + stream tail) can de-duplicate instead of
    # double-counting the spend.
    usage_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    num_turns: Optional[int] = None
    elapsed_s: float = 0.0
    # Real subprocess exit status. Parsers normalize vendor output and cannot infer
    # process success from response text alone, so execution layers attach this
    # after the child exits. None means the runner could not observe an exit code.
    returncode: Optional[int] = None
    timed_out: bool = False
    # OOM-killed: the worker's process was SIGKILL'd by the kernel out-of-memory
    # killer (a sibling run's container ballooned and starved the Docker VM — no
    # per-container --memory limit). This looks IDENTICAL to a wall-clock timeout by
    # exit code alone (the in-container `timeout` wrapper propagates 128+9=137 for
    # BOTH a real timeout AND a SIGKILL'd child), so we discriminate by the cgroup
    # oom_kill counter delta and surface it as its OWN reason — a worker that died
    # at 60s with an empty transcript is an OOM victim, NOT a 2400s timeout, and
    # mislabeling it as "timeout" sent diagnosis down the wrong path.
    oom_killed: bool = False
    # Stream capture exceeded the configured stdout/stderr budget, or the workdir
    # grew past its disk budget.  Distinct from timeout so diagnostics can name
    # which ceiling was hit.
    output_limit: bool = False
    disk_limit: bool = False
    cancelled: bool = False         # killed by a cancel_event (winner found / abort)
    steered: bool = False           # ended early by a steer_event — END THIS PASS but
    #   KEEP the session id (operator hint/redirect). The worker does NOT resume on
    #   steered (no resume loop under single-shot); the guidance flows to the next
    #   spawned worker. Used only to avoid downgrading _session_established on the cut
    #   pass. Distinct from `cancelled` (= die).
    finished: bool = False          # commit-step ended this Worker normally
    raw_stderr: str = ""
    # Vendor/protocol failure detail.  Diagnostics stay diagnostics and are never
    # copied into ``text`` as a fabricated assistant response — the provenance
    # gate must not be able to read an error string as an agent reply.
    error: str = ""
    runtime_status: dict = field(default_factory=dict)


@dataclass
class StreamStep:
    """One live step parsed from a streaming CLI line — so the deck can show the
    worker thinking/acting in real time instead of a dead pause until it returns.

    kind:
      "reasoning"    — the agent's prose/thought (text block)        → REASONING_DELTA
      "tool"         — a tool/command the agent invoked              → TOOL_CALL
      "tool_result"  — that tool's output                            → TERMINAL_OUTPUT
      "session"      — the engine assigned/echoed a session id
    """
    kind: str
    text: str = ""
    tool: str = ""        # tool name (kind == "tool")
    session: str = ""     # session id (kind == "session")
    # FULL, UNTRUNCATED tool output (kind == "tool_result"). `text` is truncated to
    # 600 chars for the live deck display, but a flag/fact provenance gate MUST see
    # what the command actually printed — a flag past char 600 of a command's output
    # (or in a nested `ssh host '...'` whose remote stdout is forwarded here) is real
    # but invisible in `text` (run-75379 false-negative: the genuine DC flag04 was
    # read on a pivoted host, its output never landed in the truncated chunk or the
    # summarized CliResult.text). Empty for non-tool_result steps; callers fall back
    # to `text` when `raw` is unset.
    raw: str = ""         # untruncated tool output (kind == "tool_result")
    # FULL, UNTRUNCATED command text (kind == "tool"). Same split as ``raw`` above,
    # for the same reason: ``text`` is the deck preview, this is what gets recorded.
    #
    # ``text`` used to be the ONLY copy, clipped to 300 chars at the parser, and
    # every trace event inherited that clip. Measured 2026-11-05: of 22,819
    # recorded commands the longest was 332 chars and 8,790 were exactly 300 — so
    # not a single multi-line exploit (OGNL expression, gremlin statement, pickle
    # script, heredoc body) survived intact. That silently capped the whole
    # payload-mining effort at ~258 usable chars, and no downstream fix could
    # recover what was cut here.
    command: str = ""     # untruncated command (kind == "tool")
    call_id: str = ""     # pairs tool with tool_result when the engine exposes one
    # Cursor can replace large shell output with an outputLocation pointer. Keep it
    # metadata-only here: the pure driver must not read an engine-supplied path, and
    # path text must never masquerade as command output. CliSolver validates and reads
    # it relative to the active worker cwd, where local/container topology is known.
    spill_path: str = ""
    spill_size_bytes: int = -1
    spill_line_count: int = -1
    # True for Runtime-provided thinking/reasoning blocks. Conversation maps these
    # onto its dedicated reasoning stream; they remain excluded from the answer
    # accumulator because message_end snapshots repeat only the answer text.
    thinking: bool = False


class SecurePromptUnsupported(RuntimeError):
    """The selected CLI cannot accept a secret prompt without argv/disk exposure."""


class LaunchPurpose(str, Enum):
    """Why a CLI process is being launched."""

    WORKER = "worker"
    CONVERSATION = "conversation"
    MANAGEMENT = "management"


@dataclass(frozen=True)
class LaunchContext:
    """Launch-time policy input shared by every CLI driver.

    ``permission_mode`` is an opaque identifier owned by the selected Agent.
    An empty value means that interactive launches inherit the Agent default.
    """

    purpose: LaunchPurpose = LaunchPurpose.WORKER
    permission_mode: str = ""
    sandbox_mode: str = ""

    @property
    def interactive(self) -> bool:
        return self.purpose in {
            LaunchPurpose.CONVERSATION,
            LaunchPurpose.MANAGEMENT,
        }


WORKER_LAUNCH = LaunchContext()


def _structured_cli_error(stdout: str) -> str:
    """Extract an explicit vendor error from JSON/JSONL output.

    Several CLIs exit zero after reporting a model/provider error in-band.  Only
    error-shaped envelopes are inspected; ordinary assistant ``message`` fields
    are deliberately ignored — otherwise a normal reply that happens to mention
    an error would be reclassified as a failed turn.
    """
    documents: list[Any] = []
    try:
        documents.append(json.loads(stdout))
    except (json.JSONDecodeError, TypeError):
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                documents.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def detail(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("message", "detail", "errorMessage", "reason", "code"):
                text = detail(value.get(key))
                if text:
                    return text
            return json.dumps(value, ensure_ascii=False)[:1000]
        if isinstance(value, list):
            return "; ".join(filter(None, (detail(item) for item in value)))[:1000]
        return str(value).strip() if value is not None else ""

    for item in documents:
        if not isinstance(item, dict):
            continue
        if item.get("errorMessage"):
            return detail(item["errorMessage"])
        stop_reason = str(item.get("stopReason") or item.get("stop_reason") or "").lower()
        event_type = str(item.get("type") or "").lower()
        status = str(item.get("status") or "").lower()
        turn = item.get("turn") if isinstance(item.get("turn"), dict) else {}
        turn_status = str(turn.get("status") or "").lower()
        failed = (
            bool(item.get("is_error"))
            or stop_reason in {"error", "failed", "refusal"}
            or status in {"error", "failed"}
            or turn_status in {"error", "failed", "interrupted"}
            or event_type in {"error", "turn.failed", "turn/error", "fatal"}
            or event_type.endswith(".error")
        )
        if not failed:
            continue
        for value in (
            item.get("error"), turn.get("error"), item.get("result"),
            item.get("message"), item.get("reason"), stop_reason,
        ):
            text = detail(value)
            if text:
                return text
        return f"CLI reported {event_type or status or turn_status or 'failure'}"
    return ""


def _cli_stderr_failure_detail(stderr: str) -> str:
    """Pick a useful stderr tail, dropping launcher update chatter.

    Host wrappers such as clawgod print ``[clawgod] vX available … run 'claude
    update'`` on every invocation.  That banner is not the turn failure and must
    not replace a structured model/provider error from stdout.
    """
    kept: list[str] = []
    for line in (stderr or "").splitlines():
        text = line.strip()
        if not text:
            continue
        lower = text.lower()
        if "available (installed:" in lower and "update" in lower:
            continue
        if lower.startswith("[clawgod]") and "update" in lower:
            continue
        if "run 'claude update'" in lower or 'run "claude update"' in lower:
            continue
        kept.append(text)
    return "\n".join(kept[-12:])[-1800:]


def finalize_cli_result(
    result: CliResult,
    *,
    driver_name: str,
    stdout: str,
    stderr: str,
    returncode: Optional[int],
) -> CliResult:
    """Attach the process/protocol outcome without manufacturing reply text."""

    result.returncode = returncode
    if not result.error:
        result.error = _structured_cli_error(stdout)
    terminal = (
        result.cancelled
        or result.steered
        or result.timed_out
        or result.oom_killed
        or result.output_limit
        or result.disk_limit
    )
    if returncode not in (None, 0) and not terminal:
        # Prefer the in-band / structured error.  Only fall back to stderr when
        # stdout did not explain the failure (and after dropping update noise).
        if not result.error:
            tail = _cli_stderr_failure_detail(stderr)
            result.error = tail or f"{driver_name} exited with code {returncode}"
    if not (result.text or "").strip() and not result.error and not terminal:
        result.error = f"{driver_name} turn ended without assistant text"
    return result


_SECURE_HELP_CACHE: "dict[tuple[str, int, tuple[str, ...], tuple[str, ...]], tuple[bool, str]]" = {}
_SECURE_HELP_LOCK = threading.Lock()


def _secure_help_preflight(
    binary: str, help_args: list[str], required: tuple[str, ...],
) -> "tuple[bool, str]":
    """Verify the installed CLI advertises every flag/pipe semantic we rely on.

    This sends no model prompt and uses no credentials. The cache key includes the
    resolved binary mtime, so replacing/upgrading a CLI automatically revalidates it.
    """
    resolved = os.path.realpath(binary)
    try:
        mtime_ns = int(os.stat(resolved).st_mtime_ns)
    except OSError:
        mtime_ns = 0
    key = (resolved, mtime_ns, tuple(help_args), required)
    with _SECURE_HELP_LOCK:
        cached = _SECURE_HELP_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [binary, *help_args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = False, f"secure prompt capability probe failed: {exc}"
    else:
        help_text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
        missing = [token for token in required if token.lower() not in help_text]
        if proc.returncode != 0:
            result = False, f"secure prompt capability probe exited {proc.returncode}"
        elif missing:
            result = False, "secure prompt flags unavailable: " + ", ".join(missing)
        else:
            result = True, ""
    with _SECURE_HELP_LOCK:
        _SECURE_HELP_CACHE[key] = result
    return result


class CliDriver(abc.ABC):
    """A thin per-CLI shelled-executor adapter."""
    name: str
    # Scheduler-visible capability: exact secret context may only select drivers
    # that guarantee stdin transport AND non-persistent CLI state.
    secure_prompt_transport = False
    # A non-secret prompt may be piped without disabling the CLI's persisted
    # session. Drivers must opt in only when their print mode supports this.
    persistent_stdin_prompt = False
    # Scheduler-visible capability: ``web_access=False`` is meaningful only when
    # this transport can make the worker's native web tools unavailable.  Keep
    # this separate from endpoint choice: a Claude CLI pointed at an Anthropic-
    # compatible model endpoint still owns exactly the same local tool surface.
    offline_web_isolation = False

    # resolved once, then cached — the actual binary this driver invokes. We pin
    # to a runnable OFFICIAL install instead of bare `self.name` so a broken
    # third-party `claude` earlier on PATH can't silently take over (see
    # resolve_engine_bin). Override via MUTEKI_CLAUDE_BIN / MUTEKI_CODEX_BIN.
    _bin: Optional[str] = None

    @property
    def bin(self) -> str:
        override = os.environ.get(_ENV_OVERRIDE.get(self.name, ""), "").strip()
        if override:
            return override
        if self._bin is None:
            self._bin = resolve_engine_bin(self.name)
        return self._bin

    def new_session(self) -> Optional[str]:
        """A pre-seeded session id, or None if the engine assigns one itself."""
        return None

    def version_argv(self) -> list[str]:
        """只读设备版本探测命令；特殊桥接 Driver 可以覆写。"""
        return [self.bin, "--version"]

    def build_execute_stdin(
        self,
        prompt: str,
        session: Optional[str],
        *,
        web_access: bool = True,
        kb_access: bool = True,
        stream: bool = False,
        launch: "LaunchContext" = WORKER_LAUNCH,
    ) -> list[str]:
        """Build a fresh, non-persistent invocation whose prompt is read from stdin.

        This is deliberately a separate capability from ``build_execute``.  Exact
        operator secrets must never be smuggled through a positional argument (where
        sibling processes can read them from the process table), and the engine must
        offer a documented way to avoid persisting the resulting conversation.  A
        driver that cannot satisfy both requirements fails closed.
        """
        del prompt, session, web_access, kb_access, stream, launch
        raise SecurePromptUnsupported(
            f"{self.name} does not support non-persistent stdin prompt delivery")

    def secure_prompt_preflight(self) -> "tuple[bool, str]":
        """Capability-neutral local check for the exact-secret CLI contract."""
        return False, f"{self.name} has no secure stdin prompt capability"

    def env_extra(self) -> "dict[str, str]":
        """Engine-specific default env for every worker run (merged UNDER any
        credential overlay, so explicit account/env values always win). Default:
        nothing."""
        return {}

    def native_permission_modes(self) -> tuple[str, ...]:
        """Return the permission mode identifiers accepted by this CLI itself."""
        return ()

    def native_sandbox_modes(self) -> tuple[str, ...]:
        """Return the sandbox identifiers accepted by this CLI itself."""
        return ()

    def validate_launch_context(self, launch: "LaunchContext") -> str:
        """Validate and return the opaque native permission identifier."""
        mode = str(launch.permission_mode or "").strip()
        if launch.interactive and mode not in ("", *self.native_permission_modes()):
            raise ValueError(
                f"{self.name} unsupported native permission mode: {mode}")
        return mode

    def validate_sandbox_mode(self, launch: "LaunchContext") -> str:
        mode = str(launch.sandbox_mode or "").strip()
        if launch.interactive and mode not in ("", *self.native_sandbox_modes()):
            raise ValueError(f"{self.name} unsupported native sandbox mode: {mode}")
        return mode

    # The optional KB MCP (if configured via MUTEKI_KB_MCP_NAME) is registered at
    # user scope and inherited by every worker; to run a worker WITHOUT it we deny
    # its mcp tools by server prefix. Empty name → no prefix → nothing to deny.
    KB_TOOL_PREFIX = f"mcp__{KB_MCP_NAME}" if KB_MCP_NAME else ""

    @abc.abstractmethod
    def build_execute(
        self,
        prompt: str,
        session: Optional[str],
        *,
        web_access: bool = True,
        kb_access: bool = True,
        stream: bool = False,
        launch: "LaunchContext" = WORKER_LAUNCH,
    ) -> list[str]:
        """argv for a fresh focused run.

        web_access=False → strip the agent's internet tools (WebSearch/WebFetch)
        so a bench eval can't be contaminated by looking up a writeup.
        kb_access=False → deny the inherited optional KB MCP tools (default: the
        worker keeps the user-scope KB, if one is configured via
        MUTEKI_KB_MCP_NAME, and can dispatch to it).
        stream=True → emit one JSON event PER STEP (assistant text / tool call /
        tool result) as the run proceeds, so the deck shows live progress instead
        of a dead pause. parse_stream_line() turns each line into a StreamStep;
        parse() still produces the final CliResult from the accumulated stdout.
        """

    def parse_stream_line(self, line: str) -> Optional["StreamStep"]:
        """Turn ONE line of streaming stdout into a live StreamStep (or None to
        ignore it). Default: nothing streams. Overridden by streaming engines.

        Single-step view (the FIRST step of a line). Kept for callers/tests that want
        one representative step; the streaming runner uses parse_stream_steps() to get
        ALL steps so a multi-block message doesn't lose later blocks (#18)."""
        return None

    def parse_stream_steps(self, line: str) -> list["StreamStep"]:
        """ALL live StreamSteps a single line carries. A single assistant message can
        hold several content blocks (text + tool_use + more text); #18: returning only
        the FIRST block dropped any FOUND_FLAG / VERIFIED_FACT in a later block from
        LIVE propagation (it only resurfaced via the final parse()). Default: wrap the
        single-step parse_stream_line (correct for engines that emit at most one step
        per line, e.g. codex). claude + cursor override this to yield every block."""
        step = self.parse_stream_line(line)
        return [step] if step is not None else []

    @abc.abstractmethod
    def build_resume(
        self,
        prompt: str,
        session: str,
        *,
        web_access: bool = True,
        kb_access: bool = True,
        stream: bool = False,
        launch: "LaunchContext" = WORKER_LAUNCH,
    ) -> list[str]:
        """argv to resume `session` with a follow-up (conclude/refine) turn."""

    @abc.abstractmethod
    def parse(self, stdout: str, stderr: str) -> CliResult:
        """Normalize the engine's stdout into a CliResult."""

    # ── self-check (FE-healthcheck-page) ─────────────────────────────────────
    # The deep probe sends ONE tiny prompt and waits for the engine to answer —
    # this is what actually exercises auth/quota (a `--version` only proves the
    # binary unpacks). All three engines share the same shape via _hello_argv()
    # so the self-check is symmetric: claude no longer the only one that really
    # talks to its backend while codex/cursor merely checked a version string.
    HELLO_PROMPT = "Reply with exactly: OK"
    # DeepSeek-via-Anthropic cold turns (esp. thinking models) routinely exceed a
    # 30s budget; a single-shot 60s probe false-fails the roster under load.
    _HELLO_TIMEOUT = 120     # one cold turn can take ~18s; leave generous headroom
    _HELLO_RETRIES = 2       # retry on a transient miss before calling it dead

    def _hello_argv(self) -> list[str]:
        """argv for a minimal one-turn 'say hello' probe. Engines that can't run a
        real turn cheaply return [] (→ fall back to the `--version` liveness check)."""
        return []

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        """Did the hello turn actually produce a model reply? Default: exit 0 and
        SOME non-empty stdout. Engines with a structured envelope tighten this."""
        return r.returncode == 0 and bool((r.stdout or "").strip())

    def healthcheck(self, *, env: "dict[str, str] | None" = None) -> bool:
        """Cheap-but-real liveness probe — can this CLI complete a turn right now
        (auth + quota ok)? Returns bool for back-compat; health_detail() carries
        the human-readable reason."""
        # Only forward env when set, so a health_detail override/stub that predates
        # the env parameter (no **kwargs) still works through the bool entrypoint.
        if env is None:
            return self.health_detail()[0]
        return self.health_detail(env=env)[0]

    def health_detail(self, *, env: "dict[str, str] | None" = None) -> "tuple[bool, str]":
        """(healthy, detail). Sends a one-turn hello and retries once on a
        transient failure (a single cold/jittery miss shouldn't report red). The
        detail names the failure mode — timeout / non-zero exit / empty reply /
        not-found — so the self-check page can tell connectivity from auth/quota.

        `env`, when given, is the COMPLETE environment for the probe subprocess
        (callers build {**os.environ, **credential_overlay}). Passing it explicitly
        — instead of the old global os.environ overlay — is what makes concurrent
        probes safe: two engines probing in parallel no longer clobber each other's
        CURSOR_API_KEY/etc. None preserves the legacy inherit-os.environ behavior."""
        argv = self._hello_argv()
        if not argv:  # engine has no cheap dry-run → fall back to version liveness
            try:
                r = subprocess.run([self.bin, "--version"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=20, env=env)
                if r.returncode == 0:
                    return True, ""
                return False, "binary not runnable (--version failed)"
            except FileNotFoundError:
                return False, "binary not found on PATH"
            except subprocess.TimeoutExpired:
                return False, "version probe timed out"
            except Exception as e:  # noqa: BLE001
                return False, str(e)[:160]

        last = "no reply"
        for attempt in range(self._HELLO_RETRIES + 1):
            try:
                r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=self._HELLO_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                return False, "binary not found on PATH"
            except subprocess.TimeoutExpired:
                last = f"hello probe timed out (>{self._HELLO_TIMEOUT}s)"
            except Exception as e:  # noqa: BLE001
                last = str(e)[:160]
            else:
                if self._hello_ok(r):
                    return True, ""
                # classify the miss so a retry/the operator knows what happened
                if r.returncode != 0:
                    failed_detail = ""
                    if '"type":"turn.failed"' in (r.stdout or ""):
                        for line in reversed((r.stdout or "").splitlines()):
                            try:
                                ev = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if ev.get("type") != "turn.failed":
                                continue
                            err = ev.get("error") or {}
                            failed_detail = str(
                                err.get("message") if isinstance(err, dict) else err
                            )
                            break
                    detail_src = failed_detail or r.stderr or r.stdout or ""
                    tail = detail_src.strip().splitlines()
                    last = (f"hello exited {r.returncode}"
                            + (f": {tail[-1][:300]}" if tail else ""))
                else:
                    last = "hello returned no model reply"
            if attempt < self._HELLO_RETRIES:
                time.sleep(1.0)  # brief backoff, then one more shot
        return False, last


_FLAG_LINE = re.compile(r"FOUND_FLAG=\s*(\S+)")


class ClaudeCodeDriver(CliDriver):
    """`claude -p` — pre-seeds a uuid session; resumes with `-r`. Host CLI,
    --dangerously-skip-permissions (full shell), JSON output for clean parsing."""
    name = "claude"
    secure_prompt_transport = True
    offline_web_isolation = True

    def native_permission_modes(self) -> tuple[str, ...]:
        return ("acceptEdits", "auto", "bypassPermissions", "manual",
                "dontAsk", "plan")

    def _permission_flags(self, launch: LaunchContext) -> list[str]:
        mode = self.validate_launch_context(launch)
        if not launch.interactive:
            return ["--dangerously-skip-permissions"]
        return ["--permission-mode", mode] if mode else []

    def new_session(self) -> Optional[str]:
        return str(uuid.uuid4())

    # claude exposes WebSearch + WebFetch by default; deny them for a clean
    # (offline) eval so the agent can't fetch a challenge writeup.
    _WEB_TOOLS = ["WebSearch", "WebFetch"]
    _EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

    def _denied(self, *, web_access: bool, kb_access: bool) -> list[str]:
        """The --disallowed-tools list for this run (empty → flag omitted)."""
        deny: list[str] = []
        if not web_access:
            deny += self._WEB_TOOLS
        if not kb_access and self.KB_TOOL_PREFIX:
            # deny the whole inherited KB MCP by server prefix (only if one is
            # configured — KB_TOOL_PREFIX is empty when MUTEKI_KB_MCP_NAME is unset)
            deny.append(self.KB_TOOL_PREFIX)
        return ["--disallowed-tools", *deny] if deny else []

    def _mcp_isolation(self, *, kb_access: bool) -> list[str]:
        # A user can have several MCP servers, while MUTEKI_KB_MCP_NAME names at
        # most one of them. Offline evaluation must exclude every external MCP
        # without changing user configuration or hiding user Skills. Claude's
        # strict config switch does exactly that for this child process.
        if kb_access:
            return []
        return [
            "--mcp-config", self._EMPTY_MCP_CONFIG,
            "--strict-mcp-config",
        ]

    def _fmt(self, stream: bool) -> list[str]:
        # stream-json emits one event per step (needs --verbose); json is a single
        # final doc. Both parse the same way via parse() on accumulated stdout.
        return (["--output-format", "stream-json", "--verbose"] if stream
                else ["--output-format", "json"])

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        argv = [self.bin, "-p", *self._fmt(stream),
                *self._permission_flags(launch)]
        if session:
            argv += ["--session-id", session]
        argv += self._mcp_isolation(kb_access=kb_access)
        argv += self._denied(web_access=web_access, kb_access=kb_access)
        argv += ["--", prompt]
        return argv

    def build_execute_stdin(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        # Claude print mode reads text input from stdin when no positional prompt is
        # supplied.  --no-session-persistence is the vendor-supported disk fence;
        # ProfileDriver adds --bare for injected credentials and endpoints. The
        # base driver represents host system login and must retain Keychain access.
        # Keep a trailing `--` sentinel so profile model injection has an unambiguous
        # insertion point, but never put the prompt itself in argv.
        del prompt, session
        return [
            self.bin, "-p", *self._fmt(stream),
            *self._permission_flags(launch),
            "--no-session-persistence",
            *self._mcp_isolation(kb_access=kb_access),
            *self._denied(web_access=web_access, kb_access=kb_access),
            "--",
        ]

    def secure_prompt_preflight(self) -> "tuple[bool, str]":
        return _secure_help_preflight(
            self.bin, ["--help"],
            ("--no-session-persistence", "--print"))

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        return [self.bin, "-r", session, "-p", *self._fmt(stream),
                *self._permission_flags(launch),
                *self._mcp_isolation(kb_access=kb_access),
                *self._denied(web_access=web_access, kb_access=kb_access),
                "--", prompt]

    @staticmethod
    def _usage_breakdown(usage: dict) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """claude's `usage` block → (input, output, cache_read, cache_write).

        ``input`` counts the fresh prompt plus BOTH cache buckets, matching the
        billed-input view the deck's token column already uses.  The cache buckets
        are also returned separately so a caller that prices cache hits differently
        is not forced to back them out of the total.  All four are None when the
        block is absent.
        """
        if not isinstance(usage, dict) or not usage:
            return None, None, None, None
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        inp = int(usage.get("input_tokens") or 0) + cache_read + cache_write
        outp = int(usage.get("output_tokens") or 0)
        return (inp or None), (outp or None), (cache_read or None), (cache_write or None)

    @staticmethod
    def _usage_tokens(usage: dict) -> tuple[Optional[int], Optional[int]]:
        """``(input, output)`` view of :meth:`_usage_breakdown`."""
        inp, outp, _cache_read, _cache_write = ClaudeCodeDriver._usage_breakdown(usage)
        return inp, outp

    def parse(self, stdout: str, stderr: str) -> CliResult:
        result = self._parse_result(stdout, stderr)
        # Cache buckets are only meaningful from the LAST usage-bearing event; the
        # result envelope often omits them even when the stream carried them.
        latest: dict = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                latest = event["usage"]
        if latest:
            result.cache_read_tokens = latest.get("cache_read_input_tokens")
            result.cache_write_tokens = latest.get("cache_creation_input_tokens")
        return result

    def _parse_result(self, stdout: str, stderr: str) -> CliResult:
        # Plain --output-format json: one JSON document.
        try:
            d = json.loads(stdout)
            inp, outp, cache_read, cache_write = self._usage_breakdown(d.get("usage") or {})
            text = str(d.get("result", ""))
            error = ""
            if d.get("is_error"):
                # Vendor error text lives in ``result``; do not project it as an
                # assistant reply.
                error = text.strip() or _structured_cli_error(stdout)
                text = ""
            return CliResult(
                text=text,
                session=d.get("session_id"),
                cost_usd=d.get("total_cost_usd"),
                input_tokens=inp,
                output_tokens=outp,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                num_turns=d.get("num_turns"),
                raw_stderr=stderr[-2000:],
                error=error or _structured_cli_error(stdout),
            )
        except json.JSONDecodeError:
            pass
        # stream-json: many JSONL lines — the final {"type":"result",...} is the
        # outcome. Scan for it (and fall back to raw text if absent).
        result_text, session, cost, turns, inp, outp = "", None, None, None, None, None
        result_is_error = False
        estimated = False
        message_usage: dict = {}
        # fallback usage from the LAST intermediate assistant message — a worker
        # KILLED mid-run (race loser / steer) never emits the final `result`, but
        # each assistant event carries a cumulative `usage` block, so the latest
        # one is the best estimate of what it burned. Without this, killed claude
        # workers report 0 tokens and their spend silently vanishes from the ledger.
        stream_in = stream_out = None
        assistant_chars = 0
        tool_use_count = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result_text = str(ev.get("result", ""))
                result_is_error = bool(ev.get("is_error"))
                cost = ev.get("total_cost_usd")
                turns = ev.get("num_turns")
                inp, outp = self._usage_tokens(ev.get("usage") or {})
            if ev.get("type") == "assistant":
                msg = ev.get("message") or {}
                si, so = self._usage_tokens(msg.get("usage") or {})
                # Keyed by message id: a repeated snapshot of the same message must
                # not be summed twice into the estimate.
                key = str(msg.get("id") or json.dumps(msg, sort_keys=True))
                message_usage[key] = (si, so)
                stream_in = sum(pair[0] or 0 for pair in message_usage.values()) if any(pair[0] is not None for pair in message_usage.values()) else None
                stream_out = sum(pair[1] or 0 for pair in message_usage.values()) if any(pair[1] is not None for pair in message_usage.values()) else None
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    bt = str(block.get("type") or "")
                    if bt == "text":
                        assistant_chars += len(str(block.get("text") or ""))
                    elif bt == "tool_use":
                        tool_use_count += 1
                        assistant_chars += len(json.dumps(block.get("input") or {}))
            if ev.get("session_id"):
                session = ev["session_id"]
        if inp is None and outp is None:  # no final result → use the streamed estimate
            inp, outp = stream_in, stream_out
        # Final result sometimes reports output_tokens=0 while stream usage (or
        # visible assistant/tool content) proves the model produced tokens. Prefer
        # the non-zero stream estimate so VOID cells don't fake "no output".
        if (outp is None or int(outp or 0) == 0) and stream_out:
            outp = stream_out
        if (inp is None or int(inp or 0) == 0) and stream_in:
            inp = stream_in
        if (outp is None or int(outp or 0) == 0) and (assistant_chars or tool_use_count):
            # Last-resort estimate when the vendor omitted usage on a killed turn.
            estimated = True
            outp = max(1, (assistant_chars // 4) + (tool_use_count * 32))
        if (inp is None or int(inp or 0) == 0) and outp:
            # Input is unknown; keep a conservative floor so cost.record can fire.
            estimated = True
            inp = int(outp)
        error = _structured_cli_error(stdout)
        if result_is_error:
            error = (result_text.strip() or error)
            result_text = ""
        return CliResult(text=result_text, session=session, cost_usd=cost,
                         usage_estimated=estimated,
                         input_tokens=inp, output_tokens=outp,
                         num_turns=turns, raw_stderr=stderr[-2000:],
                         error=error)

    def parse_stream_line(self, line: str) -> Optional[StreamStep]:
        # single-step view (first step of the line); see parse_stream_steps for the
        # all-blocks version the streaming runner uses.
        steps = self.parse_stream_steps(line)
        return steps[0] if steps else None

    def parse_stream_steps(self, line: str) -> list[StreamStep]:
        # #18: a claude assistant message can carry MULTIPLE content blocks (text +
        # tool_use + more text); emit a StreamStep for EVERY block so a FOUND_FLAG /
        # VERIFIED_FACT in a later block propagates live, not only via final parse().
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        t = ev.get("type")
        if t == "system" and ev.get("session_id"):
            return [StreamStep("session", session=ev["session_id"])]
        steps: list[StreamStep] = []
        if t == "assistant":
            for b in (ev.get("message", {}) or {}).get("content", []) or []:
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    steps.append(StreamStep("reasoning", text=b["text"].strip()))
                elif bt == "thinking":
                    thinking = str(b.get("thinking") or b.get("text") or "").strip()
                    if thinking:
                        steps.append(StreamStep(
                            "reasoning", text=thinking, thinking=True))
                elif bt == "tool_use":
                    inp = b.get("input", {}) or {}
                    arg = str(inp.get("command") or inp.get("query") or inp.get("file_path") or "")
                    steps.append(StreamStep(
                        "tool", tool=str(b.get("name", "")), text=arg[:300],
                        command=arg, call_id=str(b.get("id") or "")))
        elif t == "user":
            for b in (ev.get("message", {}) or {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    txt = c if isinstance(c, str) else json.dumps(c)
                    full = txt or ""
                    # text=truncated for the deck; raw=full for the provenance gate.
                    steps.append(StreamStep(
                        "tool_result", text=full[:600], raw=full,
                        call_id=str(b.get("tool_use_id") or "")))
        return steps

    def _hello_argv(self) -> list[str]:
        # one-turn JSON dry-run; _hello_ok asserts the result envelope came back.
        # Keep the minimal turn non-persistent. ProfileDriver adds --bare only for
        # injected credentials/endpoints; host system login needs Keychain reads.
        return [
            self.bin, "-p", "--output-format", "json", "--max-turns", "1",
            "--dangerously-skip-permissions", "--no-session-persistence",
            *self._mcp_isolation(kb_access=False),
            "--tools", "",
            "--", self.HELLO_PROMPT,
        ]

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        if r.returncode != 0:
            return False
        parsed = self.parse(r.stdout or "", r.stderr or "")
        return bool(parsed.text.strip()) and not parsed.error


class CodexDriver(CliDriver):
    """`codex exec` — engine assigns the session (scraped from stderr 'session id:');
    resumes with `codex exec resume <id>`. May be usage-limited (degrade to claude)."""
    name = "codex"
    # Codex CLI can burn ~100s on websocket retries before falling back to HTTPS.
    # Keep the deep probe truthful: a completed fallback turn is healthy, not red.
    _HELLO_TIMEOUT = 150
    _SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def _globals(self, *, web_access: bool) -> list[str]:
        # `--search` is a GLOBAL codex flag (before the `exec` subcommand) that
        # enables the native web_search tool. codex exec has NO web tool unless it
        # is passed → offline is the default; we only opt IN when web_access is on.
        # (The optional KB MCP lives in claude's user config, not codex's ~/.codex,
        # so codex doesn't see it — claude is the KB consumer.)
        return ["--search"] if web_access else []

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        # kb_access is a no-op for codex: the optional KB MCP is registered in
        # claude's user config, not codex's ~/.codex — codex doesn't inherit it.
        # `--json` already emits live per-step JSONL, so streaming needs no extra
        # flag — stream is accepted for interface parity.
        # codex exposes no native permission-mode vocabulary; `exec` is already
        # non-interactive, so `launch` carries no argv here.
        del launch
        return [self.bin, *self._globals(web_access=web_access),
                "exec", "--json", "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                "--", prompt]

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        del launch
        return [self.bin, *self._globals(web_access=web_access),
                "exec", "resume", session, "--json",
                "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                "--", prompt]

    def parse_stream_line(self, line: str) -> Optional[StreamStep]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return None
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return None
        t = ev.get("type")
        if t == "thread.started" and ev.get("thread_id"):
            return StreamStep("session", session=ev["thread_id"])
        item = ev.get("item") or {}
        it = item.get("type")
        # a shell command the agent is about to / did run
        if t == "item.started" and it == "command_execution":
            cmd = str(item.get("command", ""))
            # ``text`` stays a preview (this is what the live deck renders);
            # ``command`` carries the whole thing so the trace can be replayed.
            return StreamStep("tool", tool="shell", text=cmd[:300], command=cmd)
        if t == "item.completed":
            if it == "command_execution":
                # aggregated_output carries the command's FULL stdout/stderr — including
                # a nested `ssh host '...'` whose remote stdout the outer ssh forwards
                # here. text=truncated for the deck; raw=full for the provenance gate.
                out = str(item.get("aggregated_output") or item.get("output") or "")
                return StreamStep("tool_result", text=out[:600], raw=out)
            if it == "agent_message":
                txt = (item.get("text") or "").strip()
                if txt:
                    return StreamStep("reasoning", text=txt)
        return None

    def parse(self, stdout: str, stderr: str) -> CliResult:
        # codex --json emits JSONL events. codex 0.133–0.137 shape:
        #   {"type":"thread.started","thread_id":"<uuid>"}        ← session for resume
        #   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
        #   {"type":"turn.completed","usage":{"input_tokens":...,"cached_input_tokens":
        #      ...,"output_tokens":...,"reasoning_output_tokens":...}}
        # Subscription codex NO LONGER reports total_cost_usd, so we re-derive an
        # API-EQUIVALENT cost from the per-turn token usage (sum across turns).
        # Older shapes ({"msg":{...}}, total_cost_usd) are still tolerated.
        text, cost, turns, session = "", None, 0, None
        in_tok = cached_tok = out_tok = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                text += line + "\n"      # tolerate non-JSON lines
                continue
            et = ev.get("type")
            # session id (for a resume/conclude turn)
            if et == "thread.started" and ev.get("thread_id"):
                session = ev["thread_id"]
            # assistant output: 0.133 wraps it in item.completed → item.agent_message
            if et == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") in ("agent_message", "assistant", "message"):
                    text += str(item.get("text") or item.get("message") or "") + "\n"
            usage_event = ev if et in {"turn.completed", "response.completed"} else None
            if usage_event is None and isinstance(ev.get("msg"), dict):
                nested_type = str(ev["msg"].get("type") or "")
                if nested_type in {"turn.completed", "response.completed"}:
                    usage_event = ev["msg"]
            if usage_event is not None:
                turns += 1
                nested_response = usage_event.get("response") or {}
                u = usage_event.get("usage") or (
                    nested_response.get("usage")
                    if isinstance(nested_response, dict)
                    else {}
                ) or {}
                in_tok += int(u.get("input_tokens") or 0)
                cached_tok += int(u.get("cached_input_tokens") or 0)
                # reasoning tokens bill as output
                out_tok += int(u.get("output_tokens") or 0) + int(
                    u.get("reasoning_output_tokens") or 0)
            # legacy / alternate shapes
            msg = ev.get("msg") or ev
            if isinstance(msg, dict):
                if msg.get("type") in ("agent_message", "assistant", "message"):
                    text += str(msg.get("message") or msg.get("text") or "") + "\n"
                if "total_cost_usd" in msg:
                    cost = msg["total_cost_usd"]
        if session is None:
            m = self._SESSION_RE.search(stderr)
            if m:
                session = m.group(1)
        # Derive API-equivalent cost from tokens when codex didn't report a dollar
        # figure (the subscription path). `input_tokens` from codex INCLUDES the
        # cached portion, so split it: cached billed at the cheaper cached rate,
        # the rest at the full input rate; reasoning already folded into out_tok.
        if cost is None and (in_tok or out_tok):
            price = PRICES.get("codex", _DEFAULT_PRICE)
            fresh_in = max(0, in_tok - cached_tok)
            cost = (
                fresh_in / 1_000_000 * price.input_per_m
                + cached_tok / 1_000_000 * CODEX_CACHED_INPUT_PER_M
                + out_tok / 1_000_000 * price.output_per_m
            )
        if not text.strip() and not stdout.strip() and stderr.strip():
            tail = "\n".join(stderr.strip().splitlines()[-12:])[-1800:]
            text = f"[codex stderr]\n{tail}\n"
        return CliResult(text=text[-8000:] or stdout[-8000:], session=session,
                         cost_usd=cost, num_turns=turns or None,
                         input_tokens=(in_tok or None), output_tokens=(out_tok or None),
                         raw_stderr=stderr[-2000:])

    def _hello_argv(self) -> list[str]:
        # a real one-turn exec (offline, sandboxed) — symmetric with claude/cursor
        # so the self-check actually exercises codex auth, not just `--version`.
        return [self.bin, "exec", "--json", "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox", "--", self.HELLO_PROMPT]

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        # codex --json streams JSONL. A completed model turn proves auth/quota and
        # the backend round-trip even when late MCP/plugin shutdown noise makes the
        # process exit non-zero after stdout already contains the successful turn.
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "turn.completed":
                return True
            if ev.get("type") == "item.completed":
                item = ev.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    return True
        return False


_POLICY_REFUSAL_RE = re.compile(
    r"^\s*(?:sorry[,，:\s-]*)?(?:i\s+)?(?:"
    r"can(?:not|['\N{RIGHT SINGLE QUOTATION MARK}]t)|"
    r"won['\N{RIGHT SINGLE QUOTATION MARK}]t|will\s+not|do\s+not)\s+"
    r"(?:help|assist|comply|support)\b",
    re.IGNORECASE,
)


def _policy_refusal_error(text: str, *, used_tools: bool) -> str:
    """Classify a zero-action Cursor refusal as an execution failure.

    Cursor can exit zero with a normal-looking result envelope after declining
    the assigned Worker role. Treating that as ``explored`` closes the intent
    and lets the same profile consume later work. Keep the matcher deliberately
    narrow and require that no tool call started, so an ordinary conclusion that
    happens to mention a refusal is not reclassified.
    """
    if used_tools or not _POLICY_REFUSAL_RE.search(text or ""):
        return ""
    return "policy_refusal: cursor declined the assigned worker task"


class CursorDriver(CliDriver):
    """`cursor-agent -p` — Cursor's headless terminal agent. The engine assigns the
    session (captured from the stream's `system.init`/`result` `session_id`);
    resumes with `--resume <chatId>`. Subscription-backed (no per-run cost), full
    shell via `--force` + `--trust`. JSONL via `--output-format stream-json`.

    Caveats (handled at the swarm/driver layer, not here):
      - Cursor has NO `--disallowed-tools` equivalent, so `web_access=False` fails
        closed instead of being silently ignored — a caller that needs a clean
        offline eval must pick a transport that can actually enforce it.
      - Cursor does NOT inherit the user-scope KB MCP (that lives in
        claude's config), so `kb_access` is a no-op — claude stays the KB
        consumer.
    """
    name = "cursor"
    offline_web_isolation = False
    # optional pinned model (e.g. "sonnet-4.5-thinking"); unset → cursor's default.
    _MODEL_ENV = "MUTEKI_CURSOR_MODEL"
    # optional pinned API endpoint.  Cursor accepts `--endpoint <url>`, which is
    # how a self-hosted/relay gateway is selected instead of cursor's own cloud;
    # unset → the CLI's default endpoint.  Env-carried so the value travels with
    # the job (a model config is not the driver's global state).
    _ENDPOINT_ENV = "CURSOR_ENDPOINT"

    def native_permission_modes(self) -> tuple[str, ...]:
        return ("plan", "ask", "auto-review", "force")

    def _permission_flags(self, launch: LaunchContext) -> list[str]:
        mode = self.validate_launch_context(launch)
        if not launch.interactive:
            return ["--force"]
        if mode in ("plan", "ask"):
            return ["--mode", mode]
        return [f"--{mode}"] if mode else []

    def new_session(self) -> Optional[str]:
        # cursor assigns the chat id itself; we scrape it from the stream so a
        # resume/conclude turn can reconnect with --resume.
        return None

    def _model(self) -> list[str]:
        m = os.environ.get(self._MODEL_ENV)
        return ["--model", m] if m else []

    def _endpoint(self) -> list[str]:
        endpoint = os.environ.get(self._ENDPOINT_ENV, "").strip()
        return ["--endpoint", endpoint] if endpoint else []

    def _fmt(self, stream: bool) -> list[str]:
        # stream-json emits one NDJSON event per step; json is a single final doc.
        # We do NOT pass --stream-partial-output, so each assistant event is one
        # complete message (no per-delta de-duplication needed).
        return (["--output-format", "stream-json"] if stream
                else ["--output-format", "json"])

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        # -p (print/headless) + --force (run all commands) + --trust (skip the
        # workspace-trust prompt in headless mode). Prompt is the trailing POSITIONAL
        # arg (cursor has no `--` separator). cwd is the subprocess cwd, so no
        # explicit --workspace is needed (matches the claude/codex drivers).
        del session, kb_access
        if not web_access:
            raise RuntimeError(
                "Cursor CLI 没有可核验的原生离线 Web 工具开关；"
                "离线评测不允许改走 ACP"
            )
        return [self.bin, "-p", *self._fmt(stream),
                *self._permission_flags(launch), "--trust",
                *self._endpoint(), *self._model(), prompt]

    def build_execute_stdin(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        # Cursor's headless mode does read stdin when the positional prompt is
        # empty, but it has no --no-session-persistence/--ephemeral equivalent:
        # runChat always creates a chat store under the Cursor state root.  Exact
        # operator secrets therefore fail closed instead of being written there.
        del prompt, session, web_access, kb_access, stream, launch
        raise SecurePromptUnsupported(
            "cursor accepts headless prompts from stdin but cannot disable chat "
            "session persistence; exact secret context is unsupported")

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        del kb_access
        if not web_access:
            raise RuntimeError(
                "Cursor CLI 没有可核验的原生离线 Web 工具开关；"
                "离线评测不允许改走 ACP"
            )
        return [self.bin, "-p", *self._fmt(stream),
                *self._permission_flags(launch), "--trust",
                "--resume", session, *self._endpoint(), *self._model(), prompt]

    @staticmethod
    def _usage_breakdown(usage: dict) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """cursor's `usage` block → (input, output, cache_read, cache_write).

        cursor uses camelCase + separate cache buckets:
        {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}. Input
        counts the fresh + both cache buckets. Cost stays None — cursor is
        subscription-backed and reports no dollar figure.
        """
        if not isinstance(usage, dict) or not usage:
            return None, None, None, None
        cache_read = int(usage.get("cacheReadTokens") or 0)
        cache_write = int(usage.get("cacheWriteTokens") or 0)
        inp = int(usage.get("inputTokens") or 0) + cache_read + cache_write
        outp = int(usage.get("outputTokens") or 0)
        return (inp or None), (outp or None), (cache_read or None), (cache_write or None)

    @staticmethod
    def _usage_tokens(usage: dict) -> tuple[Optional[int], Optional[int]]:
        """``(input, output)`` view of :meth:`_usage_breakdown`."""
        inp, outp, _cache_read, _cache_write = CursorDriver._usage_breakdown(usage)
        return inp, outp

    @staticmethod
    def _tool_summary(tc: dict) -> tuple[str, str]:
        """(tool_name, FULL command/argument) from cursor's tool_call object. Shapes:
        {"readToolCall": {"args": {...}}} | {"function": {"name","arguments"}}.

        Returns the *untruncated* argument on purpose — the caller clips it for the
        deck itself.  Clipping here is what used to destroy the command before it
        could be recorded.
        """
        if not isinstance(tc, dict) or not tc:
            return ("", "")
        key = next(iter(tc))
        body = tc.get(key) or {}
        if key == "function" and isinstance(body, dict):
            return (str(body.get("name", "function")), str(body.get("arguments", "")))
        name = key[:-8] if key.endswith("ToolCall") else key  # readToolCall → read
        arg = ""
        if isinstance(body, dict) and isinstance(body.get("args"), dict):
            a = body["args"]
            arg = str(a.get("path") or a.get("command") or a.get("query") or "")
        return (name, arg)

    def parse(self, stdout: str, stderr: str) -> CliResult:
        result = CursorDriver._parse_result(self, stdout, stderr)
        # Cache buckets may only appear on an intermediate event; the terminal
        # result envelope often omits them.
        latest: dict = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                latest = event["usage"]
        if latest:
            result.cache_read_tokens = latest.get("cacheReadTokens")
            result.cache_write_tokens = latest.get("cacheWriteTokens")
        return result

    def _parse_result(self, stdout: str, stderr: str) -> CliResult:
        # --output-format json: one JSON object {type:result, result, session_id, ...}
        try:
            d = json.loads(stdout)
            if isinstance(d, dict) and (d.get("type") == "result" or "result" in d):
                inp, outp, cache_read, cache_write = self._usage_breakdown(d.get("usage") or {})
                result_text = str(d.get("result", ""))
                return CliResult(
                    text=result_text,
                    session=d.get("session_id"),
                    cost_usd=None,         # subscription-backed; no per-run cost
                    input_tokens=inp,
                    output_tokens=outp,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    num_turns=None,
                    raw_stderr=stderr[-2000:],
                    error=_policy_refusal_error(
                        result_text, used_tools=False),
                )
        except json.JSONDecodeError:
            pass
        # stream-json: NDJSON. The terminal {"type":"result",...} carries the full
        # text + usage; any line may carry session_id (system.init or result).
        result_text, session, inp, outp = "", None, None, None
        used_tools = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result_text = str(ev.get("result", ""))
                inp, outp = self._usage_tokens(ev.get("usage") or {})
            if (ev.get("type") == "tool_call"
                    and ev.get("subtype") == "started"):
                used_tools = True
            if ev.get("session_id"):
                session = ev["session_id"]
        error = _structured_cli_error(stdout)
        if not error:
            error = _policy_refusal_error(result_text, used_tools=used_tools)
        return CliResult(text=result_text, session=session, cost_usd=None,
                         input_tokens=inp, output_tokens=outp,
                         num_turns=None, raw_stderr=stderr[-2000:],
                         error=error)

    def parse_stream_line(self, line: str) -> Optional[StreamStep]:
        # single-step view (first step of the line); see parse_stream_steps for the
        # all-blocks version the streaming runner uses.
        steps = self.parse_stream_steps(line)
        return steps[0] if steps else None

    def parse_stream_steps(self, line: str) -> list[StreamStep]:
        # #18: a cursor assistant message can carry MULTIPLE text blocks; emit one
        # StreamStep per block so a FOUND_FLAG/VERIFIED_FACT in a later block isn't
        # lost from live propagation (tool_call/system lines carry one step each).
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        t = ev.get("type")
        if t == "system" and ev.get("session_id"):
            return [StreamStep("session", session=ev["session_id"])]
        if t == "assistant":
            steps: list[StreamStep] = []
            for b in (ev.get("message", {}) or {}).get("content", []) or []:
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    steps.append(StreamStep("reasoning", text=b["text"].strip()))
                elif b.get("type") == "thinking":
                    thinking = str(b.get("thinking") or b.get("text") or "").strip()
                    if thinking:
                        steps.append(StreamStep(
                            "reasoning", text=thinking, thinking=True))
            return steps
        if t == "tool_call":
            sub = ev.get("subtype")
            tc = ev.get("tool_call") or {}
            call_id = str(ev.get("call_id") or tc.get("id") or "")
            if sub == "started":
                tool, arg = self._tool_summary(tc)
                return [StreamStep("tool", tool=tool, text=arg[:300], command=arg,
                                   call_id=call_id)]
            if sub == "completed":
                # Cursor tool families expose different success payloads. File tools
                # use content/path; shellToolCall uses interleavedOutput or stdout/stderr.
                body = tc.get(next(iter(tc))) if isinstance(tc, dict) and tc else {}
                res = (body or {}).get("result") if isinstance(body, dict) else None
                content = ""
                spill_path = ""
                spill_size_bytes = -1
                spill_line_count = -1
                if isinstance(res, dict):
                    outcome = res.get("success")
                    if not isinstance(outcome, dict):
                        outcome = res.get("failure")
                    if (not isinstance(outcome, dict)
                            and res.get("case") in {"success", "failure"}
                            and isinstance(res.get("value"), dict)):
                        outcome = res["value"]
                    if isinstance(outcome, dict):
                        content = str(outcome.get("content") or "")
                        if not content:
                            content = str(outcome.get("interleavedOutput") or "")
                        if not content:
                            content = "\n".join(
                                str(outcome.get(key) or "")
                                for key in ("stdout", "stderr")
                                if outcome.get(key))
                        if not content:
                            content = str(outcome.get("path") or "")
                        location = outcome.get("outputLocation")
                        if isinstance(location, dict):
                            spill_path = str(location.get("filePath") or "")
                            try:
                                spill_size_bytes = int(location.get("sizeBytes", -1))
                            except (TypeError, ValueError):
                                spill_size_bytes = -1
                            try:
                                spill_line_count = int(location.get("lineCount", -1))
                            except (TypeError, ValueError):
                                spill_line_count = -1
                # text=truncated for the deck; raw=full for the provenance gate. A
                # spill path remains metadata-only until CliSolver validates it.
                return [StreamStep(
                    "tool_result", text=content[:600], raw=content,
                    call_id=call_id, spill_path=spill_path,
                    spill_size_bytes=spill_size_bytes,
                    spill_line_count=spill_line_count)]
        return []

    def _hello_argv(self) -> list[str]:
        # Reuse the real worker argv so the probe exercises the same flags a live
        # turn does (--trust included, so it cannot hang on the trust prompt).
        return self.build_execute(
            self.HELLO_PROMPT, None, web_access=True, kb_access=False)

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        if r.returncode != 0:
            return False
        parsed = self.parse(r.stdout or "", r.stderr or "")
        return bool(parsed.text.strip()) and not parsed.error


DRIVERS: dict[str, CliDriver] = {
    "claude": ClaudeCodeDriver(),
    "codex": CodexDriver(),
    "cursor": CursorDriver(),
}


def get_driver(name: str) -> CliDriver:
    try:
        return DRIVERS[name]
    except KeyError:
        raise ValueError(
            f"unknown engine {name!r}: expected one of {sorted(DRIVERS)} "
            f"(a profile id like 'codex-sub-container' should be resolved to its "
            f"base engine via driver_for/base_engine_for_profile first)"
        ) from None


def _insert_model_arg(argv: list[str], model: str) -> list[str]:
    model = (model or "").strip()
    if not model or "--model" in argv or "-m" in argv:
        return argv
    if "--" in argv:
        idx = argv.index("--")
        return [*argv[:idx], "--model", model, *argv[idx:]]
    if len(argv) <= 1:
        return [*argv, "--model", model]
    return [*argv[:-1], "--model", model, argv[-1]]


class ProfileDriver(CliDriver):
    """Profile-bound wrapper for local/subscription workers.

    A worker profile is the unit the operator configures. Health probes and argv
    construction must therefore carry the profile's selected model too; otherwise a
    quota-exhausted default model can mark the whole engine unhealthy.
    """

    def __init__(self, base: CliDriver, profile: dict[str, Any]) -> None:
        self.base = base
        self.profile = dict(profile)
        self.name = base.name
        self.HELLO_PROMPT = base.HELLO_PROMPT
        self._HELLO_TIMEOUT = getattr(base, "_HELLO_TIMEOUT", self._HELLO_TIMEOUT)
        self._HELLO_RETRIES = getattr(base, "_HELLO_RETRIES", self._HELLO_RETRIES)

    @property
    def bin(self) -> str:
        return self.base.bin

    def _model(self) -> str:
        return str(self.profile.get("model") or "").strip()

    def _with_model(self, argv: list[str]) -> list[str]:
        return _insert_model_arg(argv, self._model())

    def _with_reasoning_effort(self, argv: list[str]) -> list[str]:
        """Inject Codex's profile-scoped reasoning effort configuration.

        Codex CLI accepts the same setting used by ``~/.codex/config.toml``
        through ``-c model_reasoning_effort=\"high\"``.  Keep this at the
        profile adapter boundary so the Coordinator and the canonical Worker
        protocol continue to exchange only a model profile, never CLI flags.
        Other CLI engines intentionally do not receive this Codex-only option.
        """
        if self.name != "codex":
            return argv
        effort = str(self.profile.get("reasoning_effort") or "").strip().lower()
        if effort not in {"none", "low", "medium", "high", "xhigh", "max", "ultra"}:
            return argv
        if any("model_reasoning_effort" in value for value in argv):
            return argv
        insert_at = argv.index("exec") if "exec" in argv else 1
        assignment = f"model_reasoning_effort={json.dumps(effort)}"
        return [*argv[:insert_at], "-c", assignment, *argv[insert_at:]]

    def _with_profile(self, argv: list[str]) -> list[str]:
        return self._with_reasoning_effort(self._with_model(argv))

    def new_session(self) -> Optional[str]:
        return self.base.new_session()

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        return self._with_profile(self.base.build_execute(
            prompt, session, web_access=web_access, kb_access=kb_access,
            stream=stream, launch=launch))

    def build_execute_stdin(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        # The base owns the non-persistence flag semantics; the profile wrapper
        # still has to add the model/reasoning configuration so the piped turn
        # targets the same model a positional turn would.
        return self._with_profile(self.base.build_execute_stdin(
            prompt, session, web_access=web_access, kb_access=kb_access,
            stream=stream, launch=launch))

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        return self._with_profile(self.base.build_resume(
            prompt, session, web_access=web_access, kb_access=kb_access,
            stream=stream, launch=launch))

    def secure_prompt_preflight(self) -> "tuple[bool, str]":
        return self.base.secure_prompt_preflight()

    def parse(self, stdout: str, stderr: str) -> CliResult:
        return self.base.parse(stdout, stderr)

    def parse_stream_line(self, line: str) -> Optional["StreamStep"]:
        return self.base.parse_stream_line(line)

    def parse_stream_steps(self, line: str) -> list["StreamStep"]:
        return self.base.parse_stream_steps(line)

    def _hello_argv(self) -> list[str]:
        return self._with_profile(self.base._hello_argv())  # noqa: SLF001

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        return self.base._hello_ok(r)  # noqa: SLF001


class EndpointDriver(CliDriver):
    """Profile-bound driver wrapper for custom API endpoints.

    The base driver still owns parsing and CLI-specific behavior; this wrapper
    only injects endpoint config and probes the configured endpoint directly.
    """

    def __init__(self, base: CliDriver, profile: dict[str, Any]) -> None:
        self.base = base
        self.profile = dict(profile)
        self.name = base.name

    @property
    def bin(self) -> str:
        return self.base.bin

    def new_session(self) -> Optional[str]:
        return self.base.new_session()

    def _codex_config_flags(self) -> list[str]:
        base_url = str(self.profile.get("base_url") or "").strip()
        if self.name != "codex" or not base_url:
            return []
        # Codex only accepts the Responses wire protocol.  `"chat"` was removed
        # upstream and `"chat_completions"` never deserialised at all, so a
        # legacy value stored in capabilities_json must never reach this flag --
        # codex would abort with "Error loading config.toml: unknown variant"
        # before sending a single request.
        wire_api = "responses"
        model = str(self.profile.get("model") or "").strip()
        # `name` is REQUIRED by codex: a [model_providers.X] block with no `name`
        # fails config load with "provider name must not be empty", so a custom
        # endpoint never even reaches the request. `env_key` pins which env var
        # holds the bearer token — OPENAI_API_KEY is exactly what the Credential
        # Account injection populates for codex (see _api_key / runtime_env_for_engine),
        # so codex reads the worker's endpoint key instead of silently sending none.
        flags = [
            "-c", "model_provider=muteki",
            "-c", "model_providers.muteki.name=muteki",
            "-c", f"model_providers.muteki.base_url={base_url}",
            "-c", f"model_providers.muteki.wire_api={wire_api}",
            "-c", "model_providers.muteki.env_key=OPENAI_API_KEY",
        ]
        if model:
            flags += ["-c", f"model={model}"]
        return flags

    def _with_model(self, argv: list[str]) -> list[str]:
        """Pin the configured model for engines launched with ``--model``.

        Codex carries its model inside the injected ``-c model=...`` provider
        config, so adding ``--model`` there would be wrong. Claude and Cursor are
        launched with ``--model``; without it they fall back to their own default
        model name, which an operator's relay usually does not serve -- the turn
        then fails with a confusing "selected model may not exist" instead of a
        clear endpoint error.
        """

        if self.name != "codex":
            argv = _insert_model_arg(argv, str(self.profile.get("model") or "").strip())
        if self.name == "claude":
            argv = self._with_claude_settings(argv)
        return argv

    def _with_claude_settings(self, argv: list[str]) -> list[str]:
        """Point the Claude CLI at the configured endpoint via a settings file.

        **Environment variables are not enough, and this is the trap.** The CLI
        reads ``~/.claude/settings.json`` and its ``env`` block wins over the
        process environment, so exporting ``ANTHROPIC_BASE_URL`` has no effect
        once such a file exists -- which is exactly the state a previous
        cc-switch-style tool leaves behind. The observed symptom is a 404
        "selected model may not exist" for a request that never left the machine.

        ``--settings <file>`` is the supported way in, and it must also pin the
        model: the CLI validates the name against its own built-in list before
        sending anything, so an operator's upstream model name (``deepseek-...``)
        would be rejected locally no matter how correct the endpoint is. Mapping
        the CLI's official name onto the real upstream one is the proxy's job, so
        here it is enough to declare the official name and let the proxy translate.

        **Only for a host-side CLI.** The file is written on this machine, and a
        containerised Worker cannot read a host temp path -- the argv is executed
        inside the container while the file stays outside it, so the probe would
        fail with the settings file simply missing. Inside a container there is
        also nothing to work around: the image has no ``~/.claude/settings.json``,
        which is the only reason the env vars fail on the host. So the env vars
        alone are correct there, and this returns the argv untouched.
        """

        if bool(self.profile.get("containerised")):
            return argv

        base_url = str(self.profile.get("base_url") or "").strip()
        if not base_url:
            return argv

        env: dict[str, str] = {"ANTHROPIC_BASE_URL": base_url}
        token = self._api_key()
        if token:
            # The local proxy demands a token because it binds all interfaces.
            env["ANTHROPIC_AUTH_TOKEN"] = token
            env["ANTHROPIC_API_KEY"] = token

        # Declare each tier with its official name so the CLI's own validation
        # passes; the proxy substitutes the real upstream model.
        model = str(self.profile.get("model") or "").strip()
        if model:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model

        try:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", prefix="muteki-claude-", delete=False, encoding="utf-8"
            )
            json.dump({"env": env}, handle)
            handle.close()
        except OSError:
            # A settings file we cannot write is not worth failing the turn over:
            # fall through with the environment, which still works when no user
            # settings file exists.
            return argv
        # Must land before the ``--`` prompt sentinel; anything after it is
        # treated as prompt text and the flag would be silently ignored.
        if "--" in argv:
            index = argv.index("--")
            return [*argv[:index], "--settings", handle.name, *argv[index:]]
        return [*argv, "--settings", handle.name]

    def _inject_before_exec(self, argv: list[str]) -> list[str]:
        flags = self._codex_config_flags()
        if not flags:
            return self._with_model(argv)
        try:
            idx = argv.index("exec")
        except ValueError:
            return self._with_model([argv[0], *flags, *argv[1:]])
        return self._with_model([*argv[:idx], *flags, *argv[idx:]])

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        return self._inject_before_exec(self.base.build_execute(
            prompt, session, web_access=web_access, kb_access=kb_access,
            stream=stream, launch=launch))

    def build_execute_stdin(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        return self._inject_before_exec(self.base.build_execute_stdin(
            prompt, session, web_access=web_access, kb_access=kb_access,
            stream=stream, launch=launch))

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
        launch: LaunchContext = WORKER_LAUNCH,
    ) -> list[str]:
        return self._inject_before_exec(self.base.build_resume(
            prompt, session, web_access=web_access, kb_access=kb_access,
            stream=stream, launch=launch))

    def secure_prompt_preflight(self) -> "tuple[bool, str]":
        return self.base.secure_prompt_preflight()

    def parse(self, stdout: str, stderr: str) -> CliResult:
        return self.base.parse(stdout, stderr)

    def parse_stream_line(self, line: str) -> Optional["StreamStep"]:
        return self.base.parse_stream_line(line)

    def parse_stream_steps(self, line: str) -> list["StreamStep"]:
        return self.base.parse_stream_steps(line)

    def _endpoint_probe_url(self) -> str:
        base_url = str(self.profile.get("base_url") or "").rstrip("/")
        if self.name == "claude":
            # Accept both "^<base>$" and "^<base>/v1/messages$" spellings: the
            # latter is what an Anthropic-compatible relay publishes, and blindly
            # appending produced a doubled path (HTTP 404 "Unknown path").
            if base_url.endswith("/v1/messages"):
                return base_url
            if base_url.endswith("/v1"):
                return f"{base_url}/messages"
            return f"{base_url}/v1/messages"
        if self.name == "codex":
            return f"{base_url}/responses"
        return base_url

    def _hello_argv(self) -> list[str]:
        if self.name != "codex":
            return []
        return self._inject_before_exec(self.base._hello_argv())  # noqa: SLF001

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        return self.base._hello_ok(r)  # noqa: SLF001

    def _api_key(self, env: "dict[str, str] | None" = None) -> str:
        """Resolve the endpoint API key for the health probe, mirroring how the
        real worker authenticates (#5). The old version only handled `env:NAME`,
        so a FILE-backed Credential Account (api_key_ref empty, secret stored in an
        API_KEY file) made the probe omit the auth header → false-negative health
        even though the live worker authenticates fine via runtime_env_for_engine.
        Resolution order: explicit api_key_ref (env: or file:) → the *_API_KEY_FILE
        / *_API_KEY env the credential injection already populates for this worker.

        `env` (when given) is the credential environment the caller resolved for this
        probe — read it instead of the process-global os.environ so a parallel probe
        sees ITS OWN injected key, not whatever another thread last overlaid."""
        src = env if env is not None else os.environ
        ref = str(self.profile.get("api_key_ref") or "").strip()
        if ref.startswith("env:"):
            return src.get(ref[4:], "")
        if ref.startswith("file:"):
            try:
                return Path(ref[5:]).read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        # No explicit ref → fall back to the env the Credential Account injection
        # sets for this transport: <PROVIDER>_API_KEY_FILE (file-backed) or the
        # bare <PROVIDER>_API_KEY (env-backed).
        env_name = "ANTHROPIC_API_KEY" if self.name == "claude" else "OPENAI_API_KEY"
        file_env = src.get(f"{env_name}_FILE", "").strip()
        if file_env:
            try:
                return Path(file_env).read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return src.get(env_name, "").strip()

    def health_detail(self, *, env: "dict[str, str] | None" = None) -> "tuple[bool, str]":
        base_url = str(self.profile.get("base_url") or "").strip()
        if not base_url:
            return self.base.health_detail(env=env)
        if self.name == "codex":
            # Codex custom endpoints are only ready if the actual CLI can complete
            # a turn with its Responses tool schema. A bare HTTP /responses probe
            # can pass while the real worker fails immediately, e.g. LiteLLM →
            # DeepSeek rejects Codex's `tools[].type = "namespace"`.
            probe_env = env
            key = self._api_key(env)
            if key and not (env or {}).get("OPENAI_API_KEY"):
                probe_env = {**os.environ, **(env or {}), "OPENAI_API_KEY": key}
            return CliDriver.health_detail(self, env=probe_env)
        argv = [
            "curl", "-fsS", "-X", "POST", "--max-time", "20",
            "-H", "Content-Type: application/json",
        ]
        key = self._api_key(env)
        if key:
            if self.name == "claude":
                argv += ["-H", f"x-api-key: {key}", "-H", "anthropic-version: 2023-06-01"]
            else:
                argv += ["-H", f"Authorization: Bearer {key}"]
        model = str(self.profile.get("model") or "").strip()
        if self.name == "claude":
            body = json.dumps({
                "model": model or "claude-3-5-haiku-latest",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "OK"}],
            })
        else:
            body = json.dumps({
                "model": model or "gpt-5-mini",
                "input": "OK",
                "max_output_tokens": 1,
            })
        argv += ["--data", body, self._endpoint_probe_url()]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                               env=env)
        except FileNotFoundError:
            return False, "curl binary not found"
        except subprocess.TimeoutExpired:
            return False, "endpoint probe timed out"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:160]
        if r.returncode == 0:
            return True, ""
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, "endpoint probe failed" + (f": {tail[-1][:120]}" if tail else "")


def driver_for(profile_or_name: str | dict[str, Any]) -> CliDriver:
    if isinstance(profile_or_name, dict):
        base_name = base_engine_for_profile(profile_or_name)
        base = get_driver(base_name)
        if profile_uses_endpoint(profile_or_name):
            return EndpointDriver(base, profile_or_name)
        return ProfileDriver(base, profile_or_name)
    # A bare string may be a base engine, a transport, OR a profile id like
    # "codex-sub-container". base_engine_for_profile recovers the base from any of
    # them, so a profile id no longer hits DRIVERS[...] raw (which would KeyError —
    # the "local run crashes on the -sub-container profile" bug).
    return get_driver(base_engine_for_profile(str(profile_or_name)))


# Deep auth-level liveness for the engine bar (FE-quota-display). `--version`
# (`available`) only proves the binary runs — it can't catch an expired headless
# auth (e.g. cursor-agent -p → "Authentication required" even though
# `cursor-agent status` shows logged-in). health_detail() shells a real one-turn
# hello, so it's expensive: cache it on its OWN throttle (>= the deck's 60s poll)
# with last-good reuse, exactly like quota. Decorative + never blocks the bar.
_HEALTH_TTL = 55.0
_health_cache: dict = {"ts": 0.0, "data": None}


@_contextlib.contextmanager
def _patched_env(values: "dict[str, str]"):
    """Temporarily overlay os.environ with `values`, restoring on exit."""
    old = {k: os.environ.get(k) for k in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _probe_health_with_creds(name: str, drv: "CliDriver",
                             account_root: "Optional[str]") -> "tuple[bool, str]":
    """Run a driver's health_detail() with the engine's DEFAULT-account credential
    env injected (when account_root is known) — so the global probe matches what a
    live worker sees. Critical for cursor: its headless CLI authenticates ONLY via
    CURSOR_API_KEY, so a bare probe falsely reports "Authentication required" and the
    engine bar shows a healthy engine as down. account_root=None → bare probe
    (no account store available, e.g. a TUI/test context)."""
    if account_root is None:
        return drv.health_detail()
    try:
        from muteki.solver.credential_accounts import runtime_env_for_engine
        # Local Codex subscription auth is the host's default CODEX_HOME
        # (~/.codex). A stale persisted codex-main account must not make the engine
        # bar or dispatch preflight report Codex down when the host login works.
        account_id = "" if name == "codex" else None
        env = runtime_env_for_engine(
            name, account_root=account_root, account_id=account_id, container=False).env
    except Exception:
        env = {}
    if not env:
        return drv.health_detail()
    with _patched_env(env):
        return drv.health_detail()


def engine_liveness(account_root: "Optional[str]" = None) -> dict:
    """Best-effort {engine: {healthy: bool, detail: str}} from a DEEP one-turn
    probe, throttled to one real run per _HEALTH_TTL with last-good reuse. This is
    what lets the engine bar show "cursor unavailable: Authentication required"
    instead of a green dot, even when no run is active. NEVER raises / blocks.

    `account_root` (the credential-account store) lets the probe inject each engine's
    default-account auth so cursor (CURSOR_API_KEY-only headless) isn't falsely
    reported down — mirrors the live-worker / _healthy_engines credential path."""
    now = time.time()
    cached = _health_cache.get("data")
    if cached is not None and now - _health_cache["ts"] < _HEALTH_TTL:
        return cached
    out: dict = {}
    for name, drv in DRIVERS.items():
        try:
            healthy, detail = _probe_health_with_creds(name, drv, account_root)
        except Exception as exc:  # noqa: BLE001 — bar must never break
            healthy, detail = False, str(exc)[:160]
        out[name] = {"healthy": bool(healthy), "detail": detail or ""}
    _health_cache["data"] = out
    _health_cache["ts"] = now
    return out


def _claude_oauth() -> "Optional[tuple[str, int]]":
    """(access_token, expires_at_ms) from env / macOS Keychain / creds file.
    Returns None when no credential is found. Used by credential_accounts for
    login detection. Never raises."""
    env_tok = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
               or os.environ.get("ANTHROPIC_AUTH_TOKEN")
               or os.environ.get("ANTHROPIC_API_KEY"))
    if env_tok and env_tok.strip():
        return env_tok.strip(), 0
    raw: Optional[str] = None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            raw = r.stdout.strip()
    except Exception:
        pass
    if not raw:
        try:
            p = Path.home() / ".claude" / ".credentials.json"
            if p.exists():
                raw = p.read_text()
        except Exception:
            pass
    if not raw:
        return None
    try:
        d = json.loads(raw)
        o = d.get("claudeAiOauth") or d
        tok = o.get("accessToken")
        exp = int(o.get("expiresAt") or 0)
        if tok:
            return tok, exp
    except Exception:
        pass
    return None


def _cursor_session_cookie() -> "Optional[str]":
    """`WorkosCursorSessionToken=<userId>::<JWT>` from the macOS Keychain +
    cli-config, or None. Never raises. (Linux Cursor stores the token elsewhere;
    we only support the Keychain path today → None elsewhere.)"""
    tok: Optional[str] = None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "cursor-access-token", "-w"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            tok = r.stdout.strip()
    except Exception:
        pass
    if not tok:
        return None
    uid: Optional[str] = None
    try:
        cfg = Path.home() / ".cursor" / "cli-config.json"
        if cfg.exists():
            uid = str(json.loads(cfg.read_text()).get("authInfo", {}).get("userId") or "")
    except Exception:
        pass
    if not uid:
        return None
    # cookie value is "<userId>::<JWT>", url-encoded (:: → %3A%3A)
    return f"WorkosCursorSessionToken={uid}%3A%3A{tok}"


def engine_status(account_root: "Optional[str]" = None,
                  backend: str = "local",
                  profiles: "Optional[list[dict[str, Any]]]" = None) -> list[dict]:
    """Cheap per-engine status for the deck's always-on engine bar.

    This endpoint is polled by the browser, so it must not spend model tokens. It
    only checks that the configured engine binary can start (`--version`) and
    annotates the selected worker profile/model when available. Token-spending
    model probes live in `/api/engines/health`, the model-test button, and the
    dispatch-time health gate.
    """
    profile_rows = [p for p in (profiles or []) if isinstance(p, dict)]
    if profile_rows:
        selected: list[tuple[str, dict[str, Any] | None]] = []
        seen: set[str] = set()
        for p in profile_rows:
            name = base_engine_for_profile(p)
            if name in DRIVERS and name not in seen:
                selected.append((name, p))
                seen.add(name)
    else:
        selected = [(name, None) for name in DRIVERS]
    out: list[dict] = []
    for name, profile in selected:
        drv = driver_for(profile) if profile else DRIVERS[name]
        try:
            b = drv.bin
            ok = _runs_ok(b)
        except Exception:
            b, ok = name, False
        row = {
            "engine": name,
            "bin": b,
            "available": ok,
            # None means "not deep-probed by the always-on poll". The frontend only
            # treats explicit False as degraded; run-scoped failures and on-demand
            # checks still surface their concrete reasons.
            "healthy": None,
            "health_detail": "",
        }
        if profile:
            row.update({
                "profile_id": profile.get("id") or "",
                "profile_name": profile.get("name") or profile.get("id") or name,
                "model": str(profile.get("model") or ""),
                "backend": backend,
            })
        out.append(row)
    return out


def engine_health(backend: str = "local",
                  account_root: "Optional[str]" = None,
                  profiles: "Optional[list[dict[str, Any]]]" = None) -> list[dict]:
    """A DEEP per-engine self-check (FE-healthcheck-page). `backend` selects WHAT
    is checked, because local and container exercise different things:

    - "local"     → run each driver's real healthcheck ON THE HOST (claude does a
                    1-turn dry run that exercises the host's default login + auth).
                    Answers "is the host's default CLI healthy?".
    - "container" → `docker run --rm` the worker image and verify each engine's
                    CLI launches INSIDE the container (image present + binary on
                    the container PATH). Answers "can the worker image actually
                    start each engine?". Auth-in-container is account-specific and
                    is covered by the per-account connectivity test, not here.

    When `profiles` is provided for local mode, self-check those configured worker
    profiles instead of the bare engines: that makes the button exercise the same
    credential account and selected model a real worker will use. Returns {engine,
    bin, version, healthy, detail, backend}. On-demand only."""
    if (backend or "").strip() == "container":
        return _engine_health_container()
    profile_rows = [p for p in (profiles or []) if isinstance(p, dict)]
    if profile_rows:
        from muteki.solver.credential_accounts import runtime_env_for_engine

        def _insert_model(argv: list[str], model: str) -> list[str]:
            model = (model or "").strip()
            if not model or "--model" in argv or "-m" in argv:
                return argv
            if "--" in argv:
                idx = argv.index("--")
                return [*argv[:idx], "--model", model, *argv[idx:]]
            if len(argv) <= 1:
                return [*argv, "--model", model]
            return [*argv[:-1], "--model", model, argv[-1]]

        out: list[dict] = []
        for profile in profile_rows:
            name = base_engine_for_profile(profile)
            drv = driver_for(profile)
            b, version, healthy, detail = name, "", False, ""
            try:
                b = drv.bin
                r = subprocess.run([b, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
                raw = (r.stdout or r.stderr or "").strip()
                version = raw.splitlines()[0][:80] if raw else ""
                if r.returncode != 0:
                    detail = "binary not runnable (--version failed)"
                else:
                    account_id = str(profile.get("credential_account") or "").strip()
                    resolved_account_id = account_id if account_id else ""
                    env = runtime_env_for_engine(
                        name,
                        account_root=Path(account_root) if account_root else None,
                        account_id=resolved_account_id,
                        container=False,
                    ).env
                    old = {k: os.environ.get(k) for k in env}
                    try:
                        os.environ.update(env)
                        if profile_uses_endpoint(profile):
                            healthy, detail = drv.health_detail()
                        else:
                            argv = _insert_model(
                                drv._hello_argv(),  # noqa: SLF001 - self-check mirrors driver probe.
                                str(profile.get("model") or ""))
                            if not argv:
                                healthy, detail = False, "driver has no hello probe"
                            else:
                                rr = subprocess.run(
                                    argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=getattr(drv, "_HELLO_TIMEOUT", 90))
                                healthy = bool(drv._hello_ok(rr))  # noqa: SLF001
                                if not healthy:
                                    tail = (rr.stderr or rr.stdout or "").strip().splitlines()
                                    detail = (f"hello exited {rr.returncode}"
                                              + (f": {tail[-1][:120]}" if tail else ""))
                    finally:
                        for k, v in old.items():
                            if v is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = v
            except FileNotFoundError:
                detail = "binary not found on PATH"
            except subprocess.TimeoutExpired:
                detail = "probe timed out"
            except Exception as e:  # noqa: BLE001
                detail = str(e)[:160]
            out.append({"engine": name, "profile_id": profile.get("id") or "",
                        "profile_name": profile.get("name") or profile.get("id") or name,
                        "model": str(profile.get("model") or ""),
                        "bin": b, "version": version, "healthy": healthy,
                        "detail": detail, "backend": "local",
                        "bin_source": resolve_engine_bin_source(name),
                        "bin_env": _ENV_OVERRIDE.get(name, "")})
        return out
    out: list[dict] = []
    for name, drv in DRIVERS.items():
        b, version, healthy, detail = name, "", False, ""
        try:
            b = drv.bin
            r = subprocess.run([b, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            raw = (r.stdout or r.stderr or "").strip()
            version = raw.splitlines()[0][:80] if raw else ""
            if r.returncode != 0:
                detail = "binary not runnable (--version failed)"
            else:
                # deep probe: a real one-turn hello (with one retry on a transient
                # miss). detail names the failure mode so red is actionable, not a
                # blanket "check login / quota". Inject the default-account creds so
                # cursor (CURSOR_API_KEY-only headless) isn't falsely reported down.
                healthy, detail = _probe_health_with_creds(name, drv, account_root)
        except FileNotFoundError:
            detail = "binary not found on PATH"
        except subprocess.TimeoutExpired:
            detail = "probe timed out"
        except Exception as e:  # noqa: BLE001 — surface the message to the operator
            detail = str(e)[:160]
        # bin_source tells the FE whether this path was explicitly pinned (env) or
        # auto-discovered (known-good / path) so it can warn that an unpinned local
        # default may resolve to the wrong version, and point at the env var to fix.
        out.append({"engine": name, "bin": b, "version": version,
                    "healthy": healthy, "detail": detail, "backend": "local",
                    "bin_source": resolve_engine_bin_source(name),
                    "bin_env": _ENV_OVERRIDE.get(name, "")})
    return out


# in-container worker binary per engine (mirrors container_exec._CONTAINER_BIN).
_CONTAINER_ENGINE_BIN = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "/home/kali/.local/bin/cursor-agent",
}


def _engine_health_container() -> list[dict]:
    """Container self-check: one `docker run --rm` per engine verifying the worker
    image has a launchable CLI. No account/bench mounts — this checks the image +
    binary plumbing only (auth is the per-account test's job)."""
    import shutil

    out: list[dict] = []
    docker = shutil.which("docker")
    # image presence is shared across engines — probe once.
    from muteki.solver.container_exec import WORKER_IMAGE
    image_ok = False
    image_detail = ""
    if not docker:
        image_detail = "docker not found"
    else:
        try:
            r = subprocess.run([docker, "image", "inspect", WORKER_IMAGE],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            image_ok = r.returncode == 0
            if not image_ok:
                image_detail = f"image missing: {WORKER_IMAGE}"
        except subprocess.TimeoutExpired:
            image_detail = "docker image inspect timed out"
        except Exception as e:  # noqa: BLE001
            image_detail = str(e)[:120]

    for name in DRIVERS:
        bin_in = _CONTAINER_ENGINE_BIN.get(name, name)
        healthy, version, detail = False, "", ""
        if not image_ok:
            detail = image_detail
        else:
            try:
                r = subprocess.run(
                    # the image ENTRYPOINT is the runtime supervisor (a daemon); a
                    # one-shot self-check must override it with a shell via
                    # --entrypoint, else `-lc <cmd>` becomes args to the supervisor.
                    [docker, "run", "--rm", "--network", "none",
                     "--entrypoint", "bash", WORKER_IMAGE,
                     "-lc", f"{bin_in} --version 2>&1 || echo MUTEKI_CLI_FAIL"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                raw = (r.stdout or "").strip()
                if "MUTEKI_CLI_FAIL" in raw or r.returncode != 0:
                    detail = f"{name} CLI not launchable in container"
                else:
                    healthy = True
                    version = raw.splitlines()[0][:80] if raw else ""
            except subprocess.TimeoutExpired:
                detail = "container probe timed out"
            except Exception as e:  # noqa: BLE001
                detail = str(e)[:120]
        out.append({"engine": name, "bin": bin_in, "version": version,
                    "healthy": healthy, "detail": detail, "backend": "container"})
    return out


def _descendant_pids(root_pid: int) -> "list[int]":
    """Every descendant PID of root_pid (depth-first), via `ps -axo pid=,ppid=`.

    killpg only reaches the worker's ORIGINAL process group. A child that calls
    setsid() (a backgrounded daemon, `docker run -d`'s client, an agent helper
    that detaches) becomes its own group leader and survives killpg — it gets
    reparented to init and keeps running, holding CPU / ports / a concurrency
    slot (the "worker shows closed but its process is still alive" leak). We walk
    the live ppid table to catch those escapees too. Best-effort; [] on any error.
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=10).stdout
    except Exception:
        return []
    children: "dict[int, list[int]]" = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    out_pids: "list[int]" = []
    stack = list(children.get(root_pid, []))
    seen: "set[int]" = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        out_pids.append(pid)
        stack.extend(children.get(pid, []))
    return out_pids


def _kill_proc_tree(proc: "subprocess.Popen", *, pgid: "Optional[int]" = None) -> None:
    """Kill a worker AND its full descendant tree, then REAP it.

    The CLI agent spawns helpers (curl, sh, python, docker); killing only the
    parent can leave a child holding the stdout pipe or running detached. Three
    layers, each best-effort:
      1. os.killpg(SIGKILL) on the worker's process group (start_new_session=True
         makes the worker a group leader, so this takes down everything that
         stayed in the group at once);
      2. enumerate every descendant PID via the live ppid table and SIGKILL each
         individually — this catches children that setsid()'d out of the group
         (the orphan/leak case killpg alone misses);
      3. proc.wait() to reap the parent so it doesn't linger as a <defunct>
         zombie occupying a process-table slot.
    """
    # 2 first: snapshot descendants BEFORE killpg, since killpg + reparent can
    # mutate the ppid table out from under us.
    descendants = _descendant_pids(proc.pid)
    try:
        target_pgid = pgid if pgid is not None else os.getpgid(proc.pid)
        os.killpg(target_pgid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    # reap the parent (avoid a defunct zombie). short timeout: it's been SIGKILL'd.
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


# ── worker conversation retention (report-writer input) ──────────────────────
# A Worker's CLI stream (assistant reasoning, tool calls, raw tool output) is the
# only complete record of HOW an Intent was solved; the Blackboard keeps just the
# verified conclusion and the engine's own session files live in the container's
# ephemeral HOME, so they die with `docker rm -f`. Nothing durable held it.
#
# Every execution path (bare host subprocess, legacy docker exec, and the
# in-container Runtime Control Plane) funnels its accumulated stdout/stderr
# through ``driver.parse`` on THIS host, so attaching a sink to the driver
# instance captures all three without engine-specific knowledge. The sink is set
# by the caller that owns the workspace — never inferred here.
_CONVERSATION_SINK = "_muteki_conversation_sink"
_CONVERSATION_FILE_CAP = 1_500_000    # chars kept from stdout per turn
_CONVERSATION_RUN_CAP = 48_000_000    # chars kept per run directory


def attach_conversation_sink(driver: "CliDriver", *, directory: str, label: str) -> None:
    """Attach a durable transcript sink to ONE driver instance.

    Callers that download many turns (a resume loop) share the instance, so the
    index lives on the sink: every capture is a NEW file and never overwrites an
    earlier turn of the same worker.
    """

    setattr(
        driver,
        _CONVERSATION_SINK,
        {"dir": str(directory), "label": str(label), "index": 0, "run_cap": _CONVERSATION_RUN_CAP},
    )


def capture_cli_conversation(driver: "CliDriver", stdout: str, stderr: str) -> None:
    """Persist one CLI turn's raw stream for the after-the-fact report writer.

    The stream is the ONLY complete record of how an Intent was solved — the
    Blackboard keeps just the verified conclusion. Best-effort by design: a
    capture failure must never take down a Worker, and when no sink is attached
    this is a cheap attribute miss.
    """

    sink = getattr(driver, _CONVERSATION_SINK, None)
    if not isinstance(sink, dict):
        return
    try:
        directory = Path(str(sink.get("dir") or ""))
        label = str(sink.get("label") or "worker")
        if not str(directory):
            return
        directory.mkdir(parents=True, exist_ok=True)
        cap = int(sink.get("run_cap") or _CONVERSATION_RUN_CAP)
        used = 0
        for path in directory.glob("*.log"):
            try:
                used += path.stat().st_size
            except OSError:
                continue
        if used >= cap:
            return
        index = int(sink.get("index") or 0) + 1
        sink["index"] = index
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", label)[:48] or "worker"
        target = directory / f"{stamp}-{index:03d}-{safe}.log"
        body = _trim_conversation_body(stdout, stderr)
        target.write_text(body, encoding="utf-8", errors="replace")
    except Exception:
        return


def _trim_conversation_body(stdout: str, stderr: str) -> str:
    head = "----- stdout (engine stream: reasoning / tool calls / tool output) -----\n"
    body = head + _trim_middle(stdout or "", _CONVERSATION_FILE_CAP)
    tail_errors = _trim_middle(stderr or "", 200_000)
    body += "\n----- stderr -----\n" + tail_errors
    return body


def _trim_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n...[truncated {len(text) - limit} chars]...\n{text[-tail:]}"


def detach_conversation_sink(driver: "CliDriver") -> None:
    """Clear a per-worker sink so a reused driver cannot cross-wire two runs."""

    try:
        if hasattr(driver, _CONVERSATION_SINK):
            delattr(driver, _CONVERSATION_SINK)
    except Exception:
        return


def run_cli(driver: CliDriver, argv: list[str], *, cwd: str, timeout: int,
            env: Optional[dict] = None, container: "Optional[object]" = None) -> CliResult:
    """Run a CLI driver's argv as a subprocess and parse the result. `env`, if
    given, OVERLAYS os.environ (so the worker inherits PATH etc. plus our vars).

    `container`: if a ContainerHandle is given, the worker runs INSIDE that
    isolated Docker container (can't read the host bench tree) instead of bare on
    the host. None → host subprocess (default, unchanged)."""
    if container is not None:
        from muteki.solver.container_exec import run_cli_container
        return run_cli_container(driver, argv, handle=container, cwd=cwd,
                                 timeout=timeout, env=env)
    t0 = time.time()
    run_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=run_env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        err = e.stderr if isinstance(e.stderr, str) else ""
        capture_cli_conversation(driver, out or "", err or "")
        res = driver.parse(out or "", err or "")
        res.timed_out = True
        res.elapsed_s = time.time() - t0
        return res
    capture_cli_conversation(driver, proc.stdout or "", proc.stderr or "")
    res = driver.parse(proc.stdout or "", proc.stderr or "")
    res.elapsed_s = time.time() - t0
    return res


def run_cli_streaming(
    driver: CliDriver, argv: list[str], *, cwd: str, timeout: int,
    on_step: "Callable[[StreamStep], None]", env: Optional[dict] = None,
    cancel_event: "Optional[threading.Event]" = None,
    on_proc: "Optional[Callable[[subprocess.Popen], None]]" = None,
    steer_event: "Optional[threading.Event]" = None,
    paused_event: "Optional[threading.Event]" = None,
    container: "Optional[object]" = None,
) -> CliResult:
    """Like run_cli, but reads stdout LINE BY LINE and fires on_step(StreamStep)
    for each parsed line as it arrives — so a caller can surface live progress.
    The full stdout is still accumulated and run through driver.parse() for the
    final CliResult (flag/cost/session), identical to the non-streaming path.
    `env`, if given, OVERLAYS os.environ for the subprocess.

    Runtime control (dispatcher control over a stateless worker subprocess):
      - `cancel_event`: when set, the subprocess is KILLED immediately (not just
        the asyncio task — that left the CLI agent running, see bug #2). A watcher
        thread kills it the instant the event fires, even if the model is mid-think
        and stdout is quiet (the per-line loop alone could wait minutes).
      - `on_proc`: invoked once with the live Popen so the caller can SIGSTOP /
        SIGCONT it for HITL pause/resume. The worker keeps the same PID, so a paused
        agent is genuinely frozen, not killed.
      - `paused_event`: set by the caller while the worker is SIGSTOP-frozen (HITL
        pause). The timeout is computed against wall-clock MINUS time spent paused, so
        a long operator pause can't trip the turn timeout and mislabel a deliberately
        frozen worker as `timed_out` (M7).
      - `steer_event`: like cancel, but means END THIS PASS without marking the worker
        dead — an operator hint/redirect/focus cuts the current pass so the swarm can
        respawn a worker that picks up the queued guidance. The subprocess is killed
        and res.steered=True; there is NO resume loop (single-shot), so the caller does
        not reconnect — steered only keeps the session id from being downgraded.
        cancel_event takes PRECEDENCE: a stop during a steer must still die.

    `container`: if a ContainerHandle is given, the worker runs INSIDE that
    isolated Docker container; all control (cancel/steer/pause) routes in via
    `docker exec kill`. None → host subprocess (default, unchanged).
    """
    if container is not None:
        from muteki.solver.container_exec import run_cli_streaming_container
        return run_cli_streaming_container(
            driver, argv, handle=container, cwd=cwd, timeout=timeout,
            on_step=on_step, env=env, cancel_event=cancel_event,
            on_proc=on_proc, steer_event=steer_event, paused_event=paused_event)
    import subprocess as _sp

    t0 = time.time()
    # M7: pause-aware timeout. `paused_accum` is the total wall-clock the worker spent
    # SIGSTOP-frozen by the operator; `pause_since` marks the start of the current
    # freeze (None when running). active_elapsed() subtracts paused time so a paused
    # worker can't be killed as `timed_out`. _pause_lock guards the two counters since
    # the watcher thread and the read loop both call active_elapsed().
    _pause_lock = threading.Lock()
    _pause_state = {"accum": 0.0, "since": None}  # mutated under _pause_lock

    def active_elapsed() -> float:
        """Wall-clock since t0 MINUS time spent paused. Folds the in-progress freeze
        in live so a worker paused RIGHT NOW doesn't keep accruing toward timeout."""
        now = time.time()
        if paused_event is not None and paused_event.is_set():
            with _pause_lock:
                if _pause_state["since"] is None:
                    _pause_state["since"] = now          # freeze just began
                paused = _pause_state["accum"] + (now - _pause_state["since"])
        else:
            with _pause_lock:
                if _pause_state["since"] is not None:    # freeze just ended → bank it
                    _pause_state["accum"] += now - _pause_state["since"]
                    _pause_state["since"] = None
                paused = _pause_state["accum"]
        return (now - t0) - paused

    run_env = {**os.environ, **env} if env else None
    # start_new_session=True puts the worker (and every descendant — the CLI agent
    # spawns curl/python/sh helpers) in its OWN process group. Killing just the
    # parent leaves a `sleep`/`curl` child holding the stdout pipe open, so the read
    # loop blocks until timeout (the deeper form of bug #2). We kill the whole GROUP.
    proc = _sp.Popen(argv, cwd=cwd, stdout=_sp.PIPE, stderr=_sp.PIPE, stdin=_sp.DEVNULL,
                     text=True, encoding="utf-8", errors="replace", bufsize=1, env=run_env,
                     start_new_session=True)  # line-buffered + own process group
    try:
        proc_pgid: "Optional[int]" = os.getpgid(proc.pid)
    except Exception:
        proc_pgid = None
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:
            pass

    cancelled = False
    steered = False
    timed_out = False
    # Watcher thread: kill the subprocess the moment cancel OR steer fires, AND
    # enforce the wall-clock timeout. Without it, a control signal during a long
    # model "think" (no stdout) wouldn't be observed until the next line — which may
    # never come — and, more critically, a worker that emits ZERO stdout would block
    # the `for line in proc.stdout` read loop FOREVER (the in-loop timeout check at
    # the bottom never runs because the iterator never yields). The watcher is the
    # ONLY thing that can break a silent hang, so it ALWAYS runs — its startup is
    # deliberately NOT gated on cancel/steer being present (it used to be, which left
    # a bare `run_cli_streaming(..., timeout=N)` call with no timeout enforcement at
    # all). Killing the proc tree closes stdout, which unblocks the read loop.
    watcher_stop = threading.Event()

    def _watch() -> None:
        nonlocal cancelled, steered, timed_out
        while not watcher_stop.is_set():
            # cancel takes precedence over steer: a stop during a steer must die.
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                return
            if steer_event is not None and steer_event.is_set():
                steered = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                return
            if active_elapsed() > timeout:
                # Enforce the timeout HERE: the main read loop may be blocked on a
                # silent process and can't self-time-out. Kill the tree (unblocks the
                # read loop) and mark timed_out so the result reflects it. Uses
                # pause-aware elapsed so a frozen worker isn't killed for being paused.
                timed_out = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                return
            watcher_stop.wait(0.1)

    watcher = threading.Thread(target=_watch, name="cli-control-watch", daemon=True)
    watcher.start()

    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain_stderr() -> None:
        try:
            assert proc.stderr is not None
            for err_line in proc.stderr:
                err_lines.append(err_line)
        except Exception:
            pass

    stderr_thread = threading.Thread(
        target=_drain_stderr, name="cli-stderr-drain", daemon=True)
    stderr_thread.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_lines.append(line)
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                break
            if steer_event is not None and steer_event.is_set():
                steered = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                break
            if active_elapsed() > timeout:
                _kill_proc_tree(proc, pgid=proc_pgid)
                timed_out = True
                break
            try:
                steps = driver.parse_stream_steps(line)  # #18: ALL blocks, not just first
            except Exception:
                steps = []
            for step in steps:
                try:
                    on_step(step)
                except Exception:
                    pass  # a deck-emit failure must never kill the worker
        proc.wait(timeout=max(1, timeout - int(active_elapsed())))
    except _sp.TimeoutExpired:
        _kill_proc_tree(proc, pgid=proc_pgid)
        timed_out = True
    except Exception:
        _kill_proc_tree(proc, pgid=proc_pgid)
    finally:
        watcher_stop.set()
        if watcher is not None:
            watcher.join(timeout=1)
        # Some CLIs spawn sidecars that inherit stderr and outlive the parent. A
        # blocking proc.stderr.read() here keeps the worker task alive forever even
        # though the CLI parent is gone, so drain stderr in a thread and tear down
        # any leftover process-group holders if EOF does not arrive promptly.
        stderr_thread.join(timeout=1)
        if stderr_thread.is_alive():
            _kill_proc_tree(proc, pgid=proc_pgid)
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            stderr_thread.join(timeout=1)
    stderr = "".join(err_lines)
    capture_cli_conversation(driver, "".join(out_lines), stderr or "")
    res = driver.parse("".join(out_lines), stderr or "")
    res.timed_out = timed_out
    res.cancelled = cancelled
    res.steered = steered
    res.elapsed_s = time.time() - t0
    return res
