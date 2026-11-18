from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.config import claude_proxy_endpoint, codex_proxy_endpoint
from app.core.database import SessionLocal
from app.engines.openai_compatible import OpenAICompatibleEngine
from app.engines.wire import CHAT_COMPLETIONS, normalize_wire_api
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import SolveRun
from app.schemas.challenge import normalize_asset_warranty_metadata
from app.schemas.model_config import (
    OPENAI_WIRE_PROVIDER_TYPES,
    RETIRED_PROVIDER_TYPES,
    runtime_engine_type,
)
from app.services.crypto import decrypt_api_key
from app.services.knowledge_context import clear_retriever_cache, knowledge_context_service
from app.services.methodology_hints import brief_lines
from app.solver.muteki.adapter import (
    EventBridge,
    SqlAlchemyOfficialEvidenceBridge,
)
from app.solver.muteki.adapter.cost_bridge import (
    record_muteki_reason_usage,
    record_official_worker_usage,
)
from app.solver.muteki.adapter.reason_model import CoordinatorReasonModel
from app.solver.muteki.adapter.upstream_runtime_graph import (
    create_runtime_graph,
    runtime_graph_backend,
)
from app.solver.muteki.core.orchestrator import MutekiOrchestrator, MutekiRunResult
from app.solver.muteki.events import EventType
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.insight_bus import InsightBus
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.recon.breadth_scanner import _public_demo_credentials
from app.solver.muteki.recon.fingerprint import classify_challenge
from app.solver.muteki.runtime.configuration import runtime_selection_from_hints
from app.solver.muteki.strategy import MutekiStrategyPlanner
from app.solver.muteki.workers import EngineProfile, WorkerJob, WorkerOutcome
from muteki.solver.credential_accounts import engine_account_id

MUTEKI_CONTAINER_ONLY_BACKEND = "upstream_container"
logger = logging.getLogger(__name__)

# A Run-level deadline is a cooperative stop, and ``Coordinator.run`` owns its
# terminal ``Finalize`` in a ``finally`` block.  Runtime therefore waits for
# that normal return path instead of cancelling it: cancelling the Orchestrator
# Task delivers the stop request *inside* Finalize, where the Coordinator can
# only defend step by step, and an inner step that consumes the cancellation
# makes Runtime report a normal result instead of stopping.  The wait is
# bounded by this grace so a genuinely wedged Finalize (for example a hung
# Sandbox teardown) cannot pin the Run open forever.  The value is deliberately
# far above a normal Finalize, which is bounded by the Worker cancellation
# grace plus one container teardown.
FINALIZE_GRACE_SECONDS = 180.0

# Last-resort bound for reclaiming the Orchestrator Task once its Finalize has
# already exceeded ``FINALIZE_GRACE_SECONDS``.  It matches the official Worker
# cancellation grace (``OfficialWorkerConfig.cancel_grace_seconds``), so an
# adapter that is already unwinding still gets to return; the Run is reported
# without a terminal handoff instead of being pinned open forever.
FINALIZE_RECLAIM_SECONDS = 15.0

# How long before the hard Run deadline the Runtime asks the Coordinator to
# CONVERGE -- stop opening new directions, finish what is already running.
#
# Without this, the deadline fired while a Worker round was mid-verification and
# that round was reaped: the Board ended up with directions that were never
# given the chance to reach a verified state.  Handing the last slice of the
# budget to the in-flight round is what makes "verification completed" reachable
# before the Run becomes terminal.
#
# The two bounds below are a single budget, not three: the Coordinator's own
# ``stop_convergence_seconds`` is seeded from this value, and the whole
# convergence wait (poll loop + Finalize fallback) is capped by it.  It must
# stay comfortably under ``FINALIZE_GRACE_SECONDS`` so Finalize still has room
# to cancel the Worker, reap the Sandbox and publish the terminal event.
RUN_CONVERGENCE_SECONDS = 120.0

# Host the Worker Sandbox dials to reach a service published on the Docker
# host's loopback. Docker Desktop forwards this name to the host, so the
# endpoint keeps its port and only the host changes.
CONTAINER_TARGET_HOST_ENV = "MUTEKI_CONTAINER_TARGET_HOST"
DEFAULT_CONTAINER_TARGET_HOST = "host.docker.internal"
# Worker backends whose engines execute inside a sibling container. Only these
# need the loopback rewrite: a chat-completions Worker issues its HTTP requests
# from this process, where 127.0.0.1 is already the right host.
CONTAINER_WORKER_BACKENDS = frozenset({"upstream_container", MUTEKI_CONTAINER_ONLY_BACKEND})
_LOOPBACK_MODEL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})

#: Mirrors the filename sanitiser in the vendored ``TemplateStore.save``.
#:
#: It replaces every run of characters outside ``[a-zA-Z0-9_-]`` with a single
#: underscore, so a name made only of non-ASCII characters collapses to ``_`` --
#: and 13 distinct solved challenges were found sharing one template file
#: because of it.  Kept in sync with the vendored expression on purpose: the
#: disambiguation below has to predict the filename the store will produce.
_TEMPLATE_FILENAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _disambiguate_template_name(name: str, challenge: Any) -> str:
    """Give a non-ASCII template name its own filename without losing the name.

    The template store de-duplicates by *filename*, which means two different
    challenges that sanitise to the same string overwrite each other -- the
    latest solve silently destroys the earlier template.  A Chinese challenge
    name is exactly that case: ``每日流量邮报：数据院碎片还原`` and a dozen others
    all sanitised to ``web___.yaml``.

    Appending a stable ASCII discriminator derived from the challenge identity
    keeps one file per challenge, while the name itself is preserved so the
    keywords (and the recalled text) still carry the original characters.
    Returns ``name`` unchanged when it already yields a usable filename.
    """

    text = str(name or "")
    if not text:
        return text
    if all(char.isascii() for char in text):
        # Already produces a meaningful ASCII filename; leave established keys
        # (and therefore existing template files) untouched.
        return text
    token = re.sub(r"[^a-zA-Z0-9]", "", str(getattr(challenge, "id", "") or ""))[:12]
    if not token:
        return text
    return f"{text} {token}"


def container_reachable_endpoint(url: str, worker_backend: str) -> str:
    """Point a loopback model endpoint at a host the Worker container can dial.

    A CLI Worker runs *inside* the run Sandbox and makes its own model calls, so
    an endpoint published on the Docker host's loopback (``http://127.0.0.1:8787``)
    is unreachable: inside the container that address is the container itself.
    Every CLI engine then fails its health probe and the Coordinator stops the
    Run with ``NO_HEALTHY_WORKER_PROFILE`` before a single Intent is created --
    the Run completes "unsolved" in seconds having done no work at all.

    Only loopback hosts are rewritten. A LAN/private address (``192.168.x.x``) is
    deliberately left alone: a bridge container reaches those directly, so
    rewriting them to the host alias would turn a working endpoint into a broken
    one. Public endpoints and Docker service names are untouched for the same
    reason.

    The replacement host is overridable through ``MUTEKI_CONTAINER_TARGET_HOST``
    (e.g. a compose service name when the backend runs inside the same network).
    """

    original = str(url or "").strip()
    if not original or worker_backend not in CONTAINER_WORKER_BACKENDS:
        return original
    parsed = urlsplit(original)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return original
    if hostname.casefold() not in _LOOPBACK_MODEL_HOSTS:
        return original
    replacement = (
        os.environ.get(CONTAINER_TARGET_HOST_ENV, "").strip() or DEFAULT_CONTAINER_TARGET_HOST
    )
    if not replacement or replacement.casefold() == hostname.casefold():
        return original
    netloc = f"[{replacement}]" if ":" in replacement and not replacement.startswith("[") else replacement
    try:
        port = parsed.port
    except ValueError:
        return original
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _claude_proxy_base_url(config: object, worker_backend: str) -> str | None:
    """The local proxy's root URL, when this config is set to route through it.

    Opt-in per config via ``capabilities_json.route_via_proxy``. It is deliberately
    not automatic: a config whose ``base_url`` already speaks Anthropic should keep
    talking to it directly, and silently rerouting one through a translation layer
    would add a hop and a failure mode for nothing.

    Host rewriting matters here. The Worker runs in a container, so the proxy's
    ``127.0.0.1`` is the *container's* loopback, not the host's -- the same trap
    ``container_reachable_endpoint`` exists for. The launcher records the
    container-facing alias alongside the port, and only the host itself uses the
    loopback spelling.
    """

    return _proxy_base_url(config, worker_backend, claude_proxy_endpoint)


def _codex_proxy_base_url(config: object, worker_backend: str) -> str | None:
    """The Codex Responses proxy's root URL, when this config routes through it.

    The exact mirror of :func:`_claude_proxy_base_url`, driven by the same
    ``route_via_proxy`` flag but resolving the Codex proxy's own state file. Two
    proxies, one per wire protocol: a relay that refuses Anthropic
    ``/v1/messages`` needs the Claude one, and a relay that refuses Responses --
    which is every chat-only relay, and the reason a ``codex_cli`` config pointed
    at one used to fail with "本站不提供 Responses(reasoning) 协议" -- needs this.

    Returns ``None`` when the flag is off or no proxy is running, so the caller
    falls back to the stored ``base_url`` and the failure surfaces as the
    upstream's own protocol error rather than a timeout against a dead port.
    """

    return _proxy_base_url(config, worker_backend, codex_proxy_endpoint)


def _proxy_base_url(config: object, worker_backend: str, endpoint_reader) -> str | None:
    """Shared body of the two proxy resolvers.

    Extracted rather than duplicated so the container-vs-host spelling rule and
    the "flag off means no proxy" rule cannot drift between the two channels --
    a drift there would send one of them to a port only the host can reach, which
    fails inside a container as an unexplained connection error.
    """

    capabilities = getattr(config, "capabilities_json", None)
    if not isinstance(capabilities, Mapping) or not capabilities.get("route_via_proxy"):
        return None
    state = endpoint_reader()
    if not state:
        # The config asks for the proxy but none is running. Returning None lets
        # the caller fall back to the stored base_url, so the failure surfaces as
        # the upstream's own auth/protocol error rather than a confusing timeout
        # against a port nothing is listening on.
        return None
    port = state.get("port")
    if not port:
        return None
    if worker_backend in CONTAINER_WORKER_BACKENDS:
        # A container cannot use the host's loopback; the launcher wrote the alias.
        return str(state.get("container_base_url") or "").rstrip("/") or None
    return str(state.get("host_base_url") or f"http://127.0.0.1:{port}").rstrip("/") or None


def anthropic_runtime_base_url(url: str) -> str:
    """Reduce a stored Claude endpoint to the root ``ANTHROPIC_BASE_URL`` expects.

    The Claude CLI appends ``/v1/messages`` itself, so a config saved as the
    full relay endpoint (``http://127.0.0.1:8787/v1/messages`` -- the spelling
    such relays publish, and the one the settings probe accepts) would make the
    CLI request ``.../v1/messages/v1/messages`` and fail with HTTP 404. The
    settings probe tolerates both spellings, so this must too, or the probe
    reports green while every real Worker turn fails.
    """

    value = str(url or "").strip().rstrip("/")
    if value.endswith("/v1/messages"):
        value = value[: -len("/v1/messages")].rstrip("/")
    if value.endswith("/v1"):
        value = value[: -len("/v1")].rstrip("/")
    return value or str(url or "").strip()


def resolve_muteki_worker_backend(configured_backend: str | None = None) -> str:
    """Resolve the only valid production Muteki Worker boundary.

    Legacy Gateway/Kali Runner and host-local CLI paths remain available to
    legacy solver modes and focused Coordinator tests. They are not valid for
    a production ``solver_mode=muteki`` Run, so configuration drift fails
    closed instead of silently crossing the old VM boundary.
    """

    value = (
        os.environ.get("APP_MUTEKI_WORKER_BACKEND", "")
        if configured_backend is None
        else configured_backend
    )
    normalized = str(value or "").strip().casefold()
    if normalized in {"", MUTEKI_CONTAINER_ONLY_BACKEND}:
        return MUTEKI_CONTAINER_ONLY_BACKEND
    if normalized in {"upstream_local", "local"}:
        return "upstream_local"
    raise ValueError(
        "MUTEKI_CONTAINER_ONLY: production Muteki requires "
        "APP_MUTEKI_WORKER_BACKEND=upstream_container; "
        f"received {normalized!r}"
    )


def _resolve_official_account_root(
    workspace_path: str,
    *,
    explicit_root: str | None = None,
) -> str | None:
    """Resolve the standard Muteki account store for a production Run.

    The official container contract projects credentials from the durable
    ``sessions/_secrets/accounts`` store.  The old adapter only consulted an
    environment variable, so an account imported through the Settings page
    was silently ignored by production Runs.  An explicit environment value
    remains authoritative; otherwise derive the sibling ``sessions`` folder
    from the Run workspace (``data/workspaces/<run-id>``).
    """

    if explicit_root and str(explicit_root).strip():
        return str(Path(explicit_root).expanduser().resolve())
    workspace = Path(workspace_path).expanduser().resolve()
    data_root = workspace.parent.parent
    account_root = data_root / "sessions" / "_secrets" / "accounts"
    if account_root.is_dir():
        return str(account_root)
    return None


@dataclass(frozen=True, slots=True)
class _ChallengeSnapshot:
    """Immutable challenge data used after ToolGateway commits the session."""

    id: str
    name: str
    description: str
    challenge_type: str
    target_url: str | None
    allowed_hosts: list[str]
    flag_pattern: str
    source_path: str | None
    metadata_json: dict[str, Any]


class MutekiRuntime:
    """Build one isolated canonical runtime for an existing SolveRun."""

    def __init__(self, session: Any, run: SolveRun, challenge: Challenge) -> None:
        self.session = session
        self.run = run
        # ToolGateway commits between worker turns.  Keep immutable runtime
        # identifiers out of expired ORM attribute access; the Blackboard and
        # Evidence adapters only need these scalar values.
        self._run_id = str(run.id)
        self._workspace_path = str(run.workspace_path)
        # Gateway commits happen between Solver turns.  Keep Runtime reads
        # detached from the SQLAlchemy Challenge instance so the next Reason
        # pass cannot trigger an async lazy load (MissingGreenlet).
        # API serialization already applies the Challenge schema's metadata
        # normalization.  The production Solver must consume that same
        # contract from the ORM boundary; otherwise adapter-backed challenges
        # lose their declared fields and silently fall back to ``query``.
        normalized_metadata = normalize_asset_warranty_metadata(challenge)
        self.challenge = _ChallengeSnapshot(
            id=str(challenge.id),
            name=str(challenge.name or ""),
            description=str(challenge.description or ""),
            challenge_type=str(challenge.challenge_type or "WEB_TARGET"),
            target_url=str(challenge.target_url) if challenge.target_url else None,
            allowed_hosts=[str(item) for item in (challenge.allowed_hosts or [])],
            flag_pattern=str(challenge.flag_pattern or r"flag\{[^}]+\}"),
            source_path=str(challenge.source_path) if challenge.source_path else None,
            metadata_json=dict(normalized_metadata or {}),
        )
        # What the Challenge itself declares about its own attack surface.
        # Computed once: it is immutable for the Run, and every Worker turn and
        # Reason pass reads the same lines, which is what lets a paired
        # knowledge_mode experiment compare two arms that were told the same
        # facts about the target.
        self._challenge_brief = brief_lines(self.challenge)
        self.event_bridge: EventBridge | None = None
        self._graph: MutekiGraph | None = None
        self._insight_bus: InsightBus | None = None
        self._public_credentials: tuple[str, str] | None = None
        self._reason_model: CoordinatorReasonModel | None = None
        self._worker_models: dict[str, OpenAICompatibleEngine] = {}

    def _exploit_first_action(self) -> str:
        """The recipe's first move, for injection into the exploit lane goal.

        Never raises: a Run must not fail because the knowledge base is missing
        or malformed.  An empty result leaves the lane exactly as it was, which
        is the correct behaviour for a box with no recipe.

        Shortened when a stop-loss gate also applies.  ``intent_goal`` is
        truncated from the tail at 600 chars, so a gate (182) plus the lane
        boilerplate (~305) plus a 190-char first action overflows by ~80 and the
        tail instruction -- "take the next recipe step, not a fresh scan" -- is
        the part that would silently disappear.  The gate is worth more than the
        extra 60 characters of step text: it is what stops a dead chain from
        eating the whole window.
        """

        try:
            from app.services.known_exploit import exploit_first_action

            limit = 110 if self._exploit_stop_loss_gate() else 190
            return exploit_first_action(self.challenge, max_chars=limit)
        except Exception:  # noqa: BLE001 - knowledge must never break a Run
            return ""

    def _exploit_probe_action(self) -> str:
        """The first move of an unmeasured recipe that still matched this box.

        Never raises, for the same reason as ``_exploit_first_action``.  Empty
        when nothing matched, which leaves the lane on the technique clause.

        Kept out of ``exploit_first_action`` on purpose: that slot carries the
        authority of a flag-proven recipe and the Coordinator words it as an
        order.  A recipe imported from vulhub has no measurement behind it, so it
        is offered as a probe the Worker is told to verify -- and only when no
        measured recipe exists.
        """

        try:
            from app.services.known_exploit import exploit_probe_action

            limit = 110 if self._exploit_stop_loss_gate() else 190
            return exploit_probe_action(self.challenge, max_chars=limit)
        except Exception:  # noqa: BLE001 - knowledge must never break a Run
            return ""

    def _exploit_first_action_provenance(self) -> tuple[str, str]:
        """``(source, verdict)`` for the FIRST ACTION, for the telemetry event.

        Never raises, for the same reason as ``_exploit_first_action``.  Both
        halves are empty when nothing was injected, which is the correct shape
        for a box with no recipe.
        """

        try:
            from app.services.known_exploit import _first_action_repair

            limit = 110 if self._exploit_stop_loss_gate() else 190
            _action, source, verdict = _first_action_repair(
                self.challenge, max_chars=limit
            )
            return source, verdict
        except Exception:  # noqa: BLE001 - knowledge must never break a Run
            return "", ""

    def _exploit_first_action_provenance_kwargs(self) -> dict[str, str]:
        """The provenance pair as orchestrator keywords, omitted when empty.

        A separate helper because the orchestrator's constructor takes explicit
        parameters and raises ``TypeError`` on an unlisted keyword -- that is
        documented in ``core/orchestrator.py`` as a hazard that kills the whole
        Run as ``MUTEKI_RUNTIME_ERROR`` with no traceback.  Passing the pair only
        when it is non-empty keeps the no-recipe path byte-identical.
        """

        source, verdict = self._exploit_first_action_provenance()
        out: dict[str, str] = {}
        if source:
            out["exploit_first_action_source"] = source
        if verdict:
            out["exploit_first_action_verdict"] = verdict
        return out

    def _exploit_stop_loss_gate(self) -> str:
        """The feasibility question for a recipe whose chain needs a callback.

        Never raises, for the same reason as ``_exploit_first_action``: knowledge
        must never break a Run.  An empty result leaves the lane goal unchanged,
        which is right for a box whose recipe is a plain request.
        """

        try:
            from app.services.known_exploit import exploit_stop_loss_gate

            return exploit_stop_loss_gate(self.challenge)
        except Exception:  # noqa: BLE001 - knowledge must never break a Run
            return ""

    async def _read_retained_output(self, evidence_refs=()) -> str:
        """The tool output this Run retained, for post-deadline flag recovery.

        The coordinator asks for a *witness*: proof that the value it is about to
        re-wrap was actually printed by the target.  The Muteki graph cannot
        answer that -- the ``upstream`` backend records lanes, intents and facts,
        while the tool text lands in the platform's ``run_events`` projection as
        ``result_excerpt``.  Measured on ``bfac5667``: the decoded ``flag.php``
        source (``//n1book{afr_1_solved}``) was in a ``result_excerpt`` while the
        graph held only the structural events.

        Async because the project's session factory is an ``async_sessionmaker``;
        bounded and never fatal, because a database hiccup must not fail Finalize.
        An empty string means "no witness", which makes the caller refuse rather
        than guess.
        """

        try:
            from sqlalchemy import text as _sql

            async with SessionLocal() as session:
                result = await session.execute(
                    _sql(
                        "SELECT payload_json FROM run_events "
                        "WHERE run_id = :run_id AND event_type = :event_type "
                        "ORDER BY sequence DESC LIMIT 400"
                    ),
                    {"run_id": self._run_id, "event_type": "muteki.worker_command"},
                )
                rows = result.all()
        except Exception:  # noqa: BLE001 - a read convenience
            return ""
        wanted = {str(ref) for ref in (evidence_refs or ()) if str(ref)}
        own: list[str] = []
        other: list[str] = []
        for (payload_json,) in rows:
            try:
                payload = json.loads(payload_json or "{}")
            except (TypeError, ValueError):
                continue
            body = payload.get("payload") if isinstance(payload, dict) else None
            if not isinstance(body, dict):
                continue
            excerpt = str(body.get("result_excerpt") or "")
            if not excerpt:
                continue
            call_id = str(body.get("call_id") or "")
            if wanted and call_id and call_id in wanted:
                own.append(excerpt)
            else:
                other.append(excerpt)
        # Facts record opaque ledger ids, so ``own`` is usually empty and the
        # whole retained output is the witness set.  That is still a witness:
        # every excerpt in it was produced by the target in this Run.
        return "\n".join(own or other)

    def _technique_candidate_goal(self) -> str:
        """The "no recipe: classify first" clause, for an unannotated box.

        Never raises, for the same reason as ``_exploit_first_action``.  Empty
        when a recipe matched -- the recipe is strictly more specific.
        """

        try:
            from app.services.known_exploit import technique_candidate_goal

            return technique_candidate_goal(self.challenge)
        except Exception:  # noqa: BLE001 - knowledge must never break a Run
            return ""

    def _seed_session_tool(self, root: Path) -> None:
        """Drop ``mkt-session`` into the workspace so a Worker can hold a session.

        Why the workspace and not the image: ``<workspace>`` is bind-mounted at
        ``/home/kali/workspace``, so a file written here is executable in the
        container on the next command with no rebuild.  Rebuilding the Worker
        image to add one script would put every Run at risk if the image is
        wrong, which is not a trade worth making for a helper.

        The gap this closes, measured 2026-11-06 over 484 Runs: ``nc`` was used
        in 71 and **55 (77%)** came back with a timeout or a dead connection,
        because a shell command sends everything at once and returns only when
        the far end closes.  Workers were rebuilding the missing layer by hand
        (``printf ... | nc``, ``tmux new-session -d -s listener 'nc -lvnp ...'``).
        CTFAgent (CCS'25) reports the same obstacle and solves it with
        line-by-line netcat sessions.

        Failure here must never stop a Run: the tool is an optimisation, and a
        Worker that cannot find it falls back to what it did before.
        """

        try:
            # ``backend/app/solver/muteki/runtime/muteki_runtime.py`` -> repo root
            # is five levels up (runtime → muteki → solver → app → backend).
            source = Path(__file__).resolve().parents[5] / "scripts" / "mkt-session.py"
            if not source.is_file():
                return
            target = root / "mkt-session"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                return
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
        except Exception:  # noqa: BLE001 - a missing helper must never break a Run
            pass

    def _seed_known_exploit_block(self, root: Path) -> None:
        """Append the curated CVE recipe and the execution checklist to ``AGENTS.md``.

        The run Sandbox mounts ``<workspace>/muteki`` as the Worker's working
        directory, and its ``AGENTS.md`` is the only file both CLI Workers
        (Codex, Claude Code) read on every turn -- the solver prompt is built by
        vendored upstream code and carries nothing from this repository's
        knowledge base.  Without this, a box whose brief is a bare
        ``https://github.com/vulhub/<cve>`` URL made the Worker spend the whole
        bounded Race window on robots.txt / header / 403 enumeration: it had no
        idea the exploit path was already written down.

        Two independent sections, each with its own idempotency marker:

        * the **execution checklist** -- platform knowledge (the flag lives in
          the target's environment) plus the "rotate the execution primitive"
          lesson, written for *every* box because neither depends on which CVE
          it is.  A box with no recipe used to get no guidance at all, which is
          how run ``418d90ec`` ended with a working shell and no flag.
        * the **recipe** -- only when a curated entry matches this Challenge.

        Appended (never overwritten) and idempotent, so a re-entrant Run does not
        stack copies.  A failure here must never stop a Run: the lane goal
        carries the same instruction, this is the belt to its braces.
        """

        try:
            from app.services.known_exploit import (
                DEADEND_DISCIPLINE,
                execution_checklist_for,
                known_exploit_block,
            )

            agents = Path(root) / "AGENTS.md"
            existing = agents.read_text(encoding="utf-8", errors="ignore") if agents.exists() else ""
            chunks: list[str] = []

            checklist_marker = "## Code execution checklist (applies to every box)"
            if checklist_marker not in existing:
                # Trimmed to the lessons this box can use.  Platform-level
                # lessons (flag hunt, confirm-first, stop condition) always
                # ship; the PHP traps only reach a PHP box, the upload
                # derivation only an upload box.  The full text is 4.7k chars
                # and more than half of it used to be noise on any given box.
                challenge = getattr(self, "challenge", None)
                chunks.append(
                    execution_checklist_for(
                        name=str(getattr(challenge, "name", "") or ""),
                        description=str(getattr(challenge, "description", "") or ""),
                        technique=str(getattr(challenge, "category", "") or ""),
                    )
                )
            # Rides with the checklist rather than inside it: the checklist says
            # how to hunt and when a box is exhausted, this says that an
            # exhaustion verdict is binding on later turns.  Separate section so
            # the idempotence markers stay independent.
            deadend_marker = "## When you write a dead-end, that direction is CLOSED"
            if deadend_marker not in existing:
                chunks.append(DEADEND_DISCIPLINE)

            # What earlier Runs against this box already worked out.  Placed
            # after the checklist and before the recipe: a box with both gets its
            # concrete payload first (below), and the prior observations only
            # fill in the reconnaissance the recipe does not describe.
            #
            # Measured why this is worth the prompt space (2026-11-08): on
            # 资产保修核验平台, 29 Runs' fact sets overlap at Jaccard 0.25-0.37 --
            # each attempt was rediscovering the same endpoints.  Across the 90
            # boxes with no recipe, 732 facts of real reconnaissance exist; they
            # were simply never carried forward.
            box_facts_marker = "## What earlier attempts on THIS box already found"
            if box_facts_marker not in existing:
                from app.services.known_exploit import box_facts_for

                prior = box_facts_for(getattr(self, "challenge", None))
                if prior:
                    chunks.append(prior)

            block = known_exploit_block(self.challenge)
            # Two kinds of section can arrive (curated, learned, or both), so the
            # idempotence check recognises either heading.  A curated-only marker
            # would let a learned-only block be appended again on every re-entry.
            #
            # The learned heading was renamed to "Already paid for on this exact
            # box" and the marker was not updated with it, so every re-entry
            # appended another 826-char copy of the same block.  Measured on
            # ``NSB_Login`` 2026-11-08: two calls produced two identical sections.
            # Both spellings are kept because a workspace seeded before the
            # rename still carries the old heading.
            markers = (
                "## Known exploit path (curated, for THIS CVE)",
                "## Known exploit path (learned from",
                "## Already paid for on this exact box",
            )
            if block and not any(marker in existing for marker in markers):
                chunks.append(block)

            if not chunks:
                return
            agents.parent.mkdir(parents=True, exist_ok=True)
            with agents.open("a", encoding="utf-8") as handle:
                handle.write("".join(chunks))
        except Exception:
            return

    def _sync_worker_claude_md(self, root: Path) -> None:
        """Mirror ``muteki/AGENTS.md`` into ``muteki/CLAUDE.md``.

        ``_seed_known_exploit_block`` writes the run's disciplines and the matched
        recipe into ``AGENTS.md``, which is **Codex's** convention.  Claude Code
        reads ``CLAUDE.md`` from its working directory, so a claude-cli Worker got
        none of it.

        Measured 2026-11-12, run ``94af75e5`` (Peizheng Group): the claude-cli
        Worker produced 454 KB of the run's actual work with ``CLAUDE.md`` at 1825
        chars of generic image text and **zero** ``VERIFIED_FACT`` markers, while
        its codex peer in the same Run emitted 14.  Its facts therefore landed
        only at Finalize -- too late to steer the next turn, which is the failure
        the "report while you work" rule exists to prevent.

        The image's ``runtime-agent`` seeds ``CLAUDE.md`` but does not overwrite an
        existing file (verified by mounting a pre-written one into the worker
        image), so this write wins; ``sync_claude_md`` prepends the environment
        summary that would otherwise be lost, and is idempotent for re-entrant
        Runs.  A failure here must never stop a Run -- Codex still has AGENTS.md,
        and the lane goal repeats the instruction.
        """

        try:
            from app.services.workspace import sync_claude_md

            agents = root / "AGENTS.md"
            if not agents.is_file():
                return
            sync_claude_md(root, agents.read_text(encoding="utf-8"))
        except Exception:
            return

    async def run_once(self, *, max_rounds: int | None = None, max_workers: int = 10) -> MutekiRunResult:
        """Run one production Muteki session in its run-scoped container.

        ``max_rounds`` remains an explicit compatibility/test override.  The
        normal production path leaves it unset so Coordinator progress is
        driven by SharedGraph changes and the persisted Run total-runtime
        limit, not by the unrelated Agent-step budget.
        """

        root = Path(self._workspace_path).resolve() / "muteki"
        self._seed_known_exploit_block(root)
        self._sync_worker_claude_md(root)
        self._seed_session_tool(root)
        # Production Muteki uses the upstream SharedGraph by default.  The
        # compatibility graph remains available through an explicit
        # APP_MUTEKI_GRAPH_BACKEND=current override for tests/legacy rollout.
        graph_backend = runtime_graph_backend(default="upstream")
        graph_path = root / "graph" / (
            "upstream_shared_graph.db" if graph_backend == "upstream" else "shared_graph.db"
        )
        # Event persistence uses a short-lived independent session because
        # graph callbacks can be scheduled while the worker session is busy
        # collecting ToolGateway results.
        self.event_bridge = EventBridge(SessionLocal, run_id=self._run_id)
        # Per-run worker Insight Bus: verified facts / dead-ends / flags are
        # fanned out to every participating worker's inbox in real time so a
        # Codex worker immediately sees what an OpenAI worker confirmed without
        # waiting for the next Coordinator poll.  The bus is memory-only and
        # per-Run, matching upstream Muteki's per-challenge fan-out hub.
        self._insight_bus = InsightBus(challenge_id=self._run_id)

        def _graph_to_insights(event) -> None:
            # The graph already fans out to the audit EventBridge; mirror the
            # same ordering here so the InsightBus is never a source of truth,
            # just a low-latency collaboration channel for active workers.
            try:
                subscriber = self.event_bridge.callback()
                if subscriber is not None and callable(subscriber):
                    subscriber(event)
            except Exception:
                if self._graph is not None:
                    with contextlib.suppress(Exception):
                        self._graph.add_dead_end(
                            actor="muteki-runtime",
                            description="EVENT_BRIDGE_CALLBACK_FAILED",
                        )
            if self._insight_bus is None:
                return
            etype = str(getattr(event, "event_type", "") or "")
            payload = dict(getattr(event, "payload", {}) or {})
            actor = str(getattr(event, "actor", "") or "worker")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if etype == "fact_added" or etype == EventType.FACT_ADDED:
                text = str(payload.get("content") or payload.get("fact") or "")
                verified = bool(getattr(event, "verified", False))
                if text and verified and loop is not None:
                    try:
                        self._graph_to_insight_task = loop.create_task(
                            self._insight_bus.fact(actor, text),
                            name=f"muteki-insight-fact-{self._run_id}",
                        )
                    except Exception:
                        pass
            elif etype == "dead_end" or etype == EventType.DEAD_END:
                text = str(payload.get("description") or payload.get("reason") or "")
                if text and loop is not None:
                    try:
                        self._graph_to_insight_task = loop.create_task(
                            self._insight_bus.dead_end(actor, text),
                            name=f"muteki-insight-deadend-{self._run_id}",
                        )
                    except Exception:
                        pass
            elif etype == "flag_found" or etype == EventType.FLAG_FOUND:
                flag = str(payload.get("flag") or "")
                if flag and loop is not None:
                    try:
                        self._graph_to_insight_task = loop.create_task(
                            self._insight_bus.flag_found(actor, flag),
                            name=f"muteki-insight-flag-{self._run_id}",
                        )
                    except Exception:
                        pass

        self._graph = create_runtime_graph(
            graph_path,
            challenge=self.challenge,
            challenge_id=self._run_id,
            event_subscriber=_graph_to_insights,
            backend=graph_backend,
        )
        reason_provider = await self._build_reason_provider()
        reason = MutekiReason(provider=reason_provider, metadata=self.challenge.metadata_json or {})
        worker_backend = resolve_muteki_worker_backend()
        native_evidence_bridge = SqlAlchemyOfficialEvidenceBridge(
            self.session,
            self.run,
            self._workspace_path,
        )
        official_account_root = _resolve_official_account_root(
            self._workspace_path,
            explicit_root=os.environ.get("MUTEKI_ACCOUNT_ROOT"),
        )
        try:
            engine_profiles = await self._worker_profiles(worker_backend)
            # Map the existing Run limits onto the official Muteki boundaries:
            # one native Worker is one durable application/tool turn, while
            # its internal CLI loop keeps the separate upstream ``max_turns``
            # limit.  Capping the smaller of the two Run budgets prevents a
            # barren route from spawning indefinitely, without putting the
            # legacy Agent/ToolGateway counters inside the official Worker.
            max_agent_steps = max(1, int(getattr(self.run, "max_agent_steps", 120) or 120))
            max_tool_calls = max(1, int(getattr(self.run, "max_tool_calls", 120) or 120))
            native_worker_budget = min(max_agent_steps, max_tool_calls)
            # Computed before the Coordinator is built because the Coordinator
            # needs the absolute deadline: without it a Worker round can start
            # that cannot finish, and the deadline then reaps it -- closing every
            # direction it was verifying as ``timed_out`` with no result.  This
            # is the same value passed to the deadline below; only the order
            # changed.
            total_timeout = max(
                1,
                int(
                    getattr(self.run, "max_total_runtime_seconds", 0)
                    or max(
                        60,
                        (max_rounds or 1)
                        * max(1, int(getattr(self.run, "max_runtime_seconds", 900) or 900)),
                    ),
                ),
            )
            orchestrator = MutekiOrchestrator(
                self._graph,
                reason,
                # The callback is deliberately fail-closed. Production
                # Muteki workers are routed by WorkerPool to the official
                # Sandbox container; no callback may re-enter ToolGateway or
                # the legacy Kali Runner.
                worker_runner=self._disabled_compat_worker,
                engines=engine_profiles,
                max_workers=max_workers,
                worker_backend=worker_backend,
                official_worker_evidence_bridge=native_evidence_bridge,
                official_worker_usage_bridge=self._record_official_worker_usage,
                official_account_root=official_account_root,
                # One Worker round's own budget, and the reason it is no longer
                # a flat 720 cap.
                #
                # Measured 2026-10-25 across nine arena runs: the worker phase
                # ended at 755-760s every time -- ``min(720, ...)`` plus launch
                # overhead -- while the Run's own budget was 1200s.  None of
                # them ran out of total time; every one was reaped mid-
                # investigation at twelve minutes, after 150-320 reconnaissance
                # steps and before the exploit request.  The cap, not the
                # budget, was the binding constraint.
                #
                # ``ARENA_WORKER_ROUND_SECONDS`` raises it for arena solves.
                # It stays a *cap* relative to the Run budget for the original
                # reason: a Worker round must not outlive the Run that owns it,
                # or the deadline below reaps it and closes every direction it
                # was still verifying as ``timed_out``.
                worker_timeout_seconds=max(
                    1,
                    min(
                        self._worker_round_cap(),
                        int(getattr(self.run, "max_runtime_seconds", 900) or 900),
                    ),
                ),
                worker_max_turns=min(80, max_agent_steps),
                max_total_workers=native_worker_budget,
                # The Coordinator must not grant itself a convergence window
                # larger than the one this Runtime reserves out of the deadline
                # below, or the Run would drift past its declared budget.
                stop_convergence_seconds=RUN_CONVERGENCE_SECONDS,
                # The absolute deadline, so the Coordinator can decline to start
                # a Worker round that cannot finish inside it.  Convergence alone
                # cannot do this: its window is bounded below the Worker budget,
                # so it only ever captures a round that is nearly done.
                run_deadline_monotonic=time.monotonic() + float(total_timeout),
                # Lifted out of the curated recipe here, where the Challenge
                # object lives.  The Coordinator only ever sees the graph (id +
                # flag pattern), so this is the last point that can resolve a
                # recipe for the box and hand its first move to the lane goal.
                exploit_first_action=self._exploit_first_action(),
                # Which recipe field the slot was read from, and why it was
                # admitted.  Only used for the ``exploit.first_action`` event: the
                # injected text itself is not otherwise persisted (Race proposes
                # no Intent), so without this an adoption audit cannot tell a
                # Worker that ignored the slot from one that never saw it.
                #
                # This MUST be ``**`` and not ``*``.  A single star unpacking an
                # iterable of pairs (or, as it was written, a dict) in a call that
                # already has keyword arguments makes Python treat the whole call
                # as positional: it failed with
                #
                #   MutekiOrchestrator.__init__() takes 3 positional arguments
                #   but 4 positional arguments (and 17 keyword-only) were given
                #
                # which the supervisor records as ``MUTEKI_RUNTIME_ERROR`` with no
                # traceback in the log -- the exact hazard ``core/orchestrator.py``
                # warns about, committed by the code that warns about it.
                **self._exploit_first_action_provenance_kwargs(),
                # Same lift point, different authority.  ``exploit_first_action``
                # is reserved for recipes that produced an accepted flag here (or
                # a hand-audited one).  This is the move from a recipe that
                # matched THIS box's CVE but was never measured -- see
                # ``known_exploit.exploit_probe_action``.  Measured 2026-11-08:
                # 1 of the 81 UNSOLVED Challenges could issue a FIRST ACTION, while
                # 12 more already match a real vulhub recipe with an executable
                # first line that was being thrown away in favour of one shared
                # "cmdi=param→shell" clause.  The two are separate parameters so a
                # reader can never mistake the probe for the measured order.
                exploit_probe_action=self._exploit_probe_action(),
                exploit_stop_loss_gate=self._exploit_stop_loss_gate(),
                technique_candidate_goal=self._technique_candidate_goal(),
                insight_bus=self._insight_bus,
                # Where the recovered flag actually lives: this Run's retained
                # tool output.  The graph holds lanes/intents/facts, not the text
                # a tool printed, so a post-deadline "re-wrap the value we saw"
                # recovery has to read the projection.
                evidence_reader=self._read_retained_output,
            )
            result = await self._run_orchestrator_with_deadline(
                orchestrator,
                max_rounds=max_rounds,
                total_timeout=total_timeout,
            )
            # Distillation (rejection sampling): only a flag-verified Run
            # becomes a reusable "case" template; a Run that ended without a
            # flag still contributes a "negative" template so the next Run
            # does not re-walk the same dead directions.
            if result.graph_path:
                self._distill(
                    result.graph_path,
                    reason=str(result.reason or ""),
                    solved=bool(result.flag_found),
                )

            try:
                await asyncio.wait_for(self.event_bridge.flush(), timeout=15.0)
            except asyncio.TimeoutError:
                self._graph.add_dead_end(actor="muteki-runtime", description="EVENT_BRIDGE_FLUSH_TIMEOUT")
            return result
        finally:
            # Graph callbacks are intentionally persisted through an ordered
            # independent-session EventBridge.  A cancelled Run must still
            # drain that tail before the graph/model handles close, otherwise
            # the final worker/recovery audit events disappear on restart.
            if self.event_bridge is not None:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(self.event_bridge.flush()),
                        timeout=15.0,
                    )
            if self._reason_model is not None:
                await self._reason_model.close()
                self._reason_model = None
            for model in tuple(self._worker_models.values()):
                await model.close()
            self._worker_models.clear()
            if self._insight_bus is not None:
                for sid in tuple(self._insight_bus._inboxes):
                    self._insight_bus.unsubscribe(sid)
            if self._graph is not None:
                self._graph.close()

    async def _run_orchestrator_with_deadline(
        self,
        orchestrator: MutekiOrchestrator,
        *,
        max_rounds: int | None,
        total_timeout: int,
    ) -> MutekiRunResult:
        """Run the Solver Loop under the total deadline.

        The deadline is spent in two stages: the last ``RUN_CONVERGENCE_SECONDS``
        is handed to the Coordinator as a *convergence* request (finish what is
        running, open nothing new), and only then does the hard stop fire.
        Without that split a Worker round that was one call away from a verified
        fact got reaped by Finalize, which is what left directions reading as
        "stopped before verification completed".

        Both stages are cooperative, not a hard task cancellation.  The
        Coordinator owns its terminal ``Finalize`` step in the normal return
        path, so Runtime must await that path to completion before reporting the
        timed-out Run.
        """

        run_task = asyncio.create_task(
            orchestrator.run(max_rounds=max_rounds),
            name=f"muteki-orchestrator-{self._run_id}",
        )
        request_convergence = getattr(
            getattr(orchestrator, "coordinator", None),
            "request_convergence",
            None,
        )
        # Converge first, stop second.  The last slice of the budget belongs to
        # whatever is already running: asking the Coordinator to converge lets
        # an in-flight Worker round finish its verification, while the hard stop
        # is still what actually ends the Run at the deadline.  A Run that
        # finishes during convergence just returns here.
        # At most a quarter of the Run budget, so convergence can never eat the
        # time a short Run needs to decide anything at all.
        convergence = min(
            RUN_CONVERGENCE_SECONDS,
            max(0.0, float(total_timeout) / 4.0),
        )
        if callable(request_convergence) and convergence > 0:
            done, _ = await asyncio.wait({run_task}, timeout=float(total_timeout) - convergence)
            if run_task in done:
                return run_task.result()
            request_convergence("MUTEKI_RUN_CONVERGING", convergence)
        try:
            return await asyncio.wait_for(
                asyncio.shield(run_task),
                timeout=convergence if convergence > 0 else total_timeout,
            )
        except asyncio.TimeoutError:
            if self._graph is not None:
                self._graph.add_dead_end(
                    actor="muteki-runtime",
                    description="MUTEKI_RUN_TIMEOUT",
                )
            request_stop = getattr(
                getattr(orchestrator, "coordinator", None),
                "request_stop",
                None,
            )
            if callable(request_stop):
                request_stop("MUTEKI_RUN_TIMEOUT")
            # Do not cancel here.  Coordinator.run owns Finalize in its
            # ``finally`` path, and handing the terminal step to a canceller
            # both risks interrupting it mid-flight and lets an inner step that
            # consumes the cancellation swallow the stop request.  The wait is
            # therefore shielded, so a Run-level stop/shutdown of this Runtime
            # cannot reach the Orchestrator Task either.
            result = await self._await_terminal_finalize(run_task)
            if result is not None:
                return result
            # Finalize outlived its grace window and had to be abandoned.  Keep
            # the Run's terminal contract explicit instead of hanging forever.
            return self._timeout_without_finalize_result()
        except BaseException:
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            raise

    async def _await_terminal_finalize(
        self,
        run_task: asyncio.Task[MutekiRunResult],
    ) -> MutekiRunResult | None:
        """Await the Coordinator's terminal Finalize despite outer cancels.

        The deadline has already been requested as a cooperative stop, so the
        Coordinator is on its way to Finalize.  A bare ``await run_task`` hands
        the Orchestrator Task to whatever stops this Runtime (Run stop request,
        service shutdown): the cancellation is then delivered *inside* Finalize,
        where the Coordinator can only defend step by step, and an inner step
        that consumes that cancellation makes this Runtime swallow the stop
        request and report a normal result instead.  ``asyncio.shield`` keeps
        the Orchestrator Task out of the canceller's reach, and the wait stays
        bounded by ``FINALIZE_GRACE_SECONDS``.

        Returns ``None`` when Finalize exceeded the grace bound and had to be
        abandoned (already recorded as an explicit dead-end).
        """

        try:
            return await asyncio.wait_for(
                asyncio.shield(run_task),
                timeout=FINALIZE_GRACE_SECONDS,
            )
        except asyncio.CancelledError:
            # This Runtime is being stopped.  Finalize is already in flight
            # inside the Coordinator, so let it finish (still shielded, still
            # bounded) before honouring the cancellation; otherwise the
            # terminal Graph event is lost exactly when it matters.
            try:
                await asyncio.wait_for(
                    asyncio.shield(run_task),
                    timeout=FINALIZE_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                await self._abandon_wedged_finalize(run_task)
            raise
        except asyncio.TimeoutError:
            await self._abandon_wedged_finalize(run_task)
            return None

    async def _abandon_wedged_finalize(
        self,
        run_task: asyncio.Task[MutekiRunResult],
    ) -> None:
        """Record and reclaim a Finalize that outlived its grace window."""

        if self._graph is not None:
            self._graph.add_dead_end(
                actor="muteki-runtime",
                description="MUTEKI_FINALIZE_GRACE_EXCEEDED",
            )
        if run_task.done():
            return
        run_task.cancel()
        # Cancelling here is a last resort, so the drain gets its own small
        # bound and deliberately does not re-await the Task: a Coordinator that
        # already consumed one cancellation can block again inside its own
        # cleanup, and this path must not become the hang it exists to prevent.
        # ``asyncio.wait`` leaves the Task alone and still propagates an outer
        # cancellation instead of absorbing it.
        await asyncio.wait({run_task}, timeout=FINALIZE_RECLAIM_SECONDS)

    def _timeout_without_finalize_result(self) -> MutekiRunResult:
        """Report a Run whose Coordinator never reached its terminal handoff.

        Mirrors ``MutekiOrchestrator.run``'s post-deadline mapping so the Run
        keeps the same TIMEOUT / COMPLETED_SOLVED contract as a cooperative
        stop instead of silently turning a timeout into an engine failure.
        """

        graph = self._graph
        flags = list(graph.flags(verified_only=True)) if graph is not None else []
        suspected_reader = getattr(graph, "suspected_flags", None)
        suspected: tuple[str, ...] = ()
        if callable(suspected_reader):
            try:
                suspected = tuple(
                    dict.fromkeys(
                        str(item.flag_value) for item in suspected_reader() if str(item.flag_value or "")
                    )
                )
            except Exception:  # noqa: BLE001 - a read convenience must not fail the Run
                suspected = ()
        return MutekiRunResult(
            run_id=self._run_id,
            challenge_id=str(getattr(graph, "challenge_id", "") or self._run_id),
            status="COMPLETED_SOLVED" if flags else "TIMEOUT",
            flag=flags[-1].flag_value if flags else None,
            flag_found=bool(flags),
            reason="FLAG_VERIFIED_AFTER_TIMEOUT" if flags else "MUTEKI_RUN_TIMEOUT",
            graph_path=str(getattr(graph, "db_path", "") or ""),
            suspected_flags=suspected,
        )

    def _on_reason_content_policy_retry(self, payload: dict) -> None:
        """Record that the Reason prompt was softened after an audit rejection.

        Emitted as its own event rather than as a fallback: the model still
        produced the plan, so a Run carrying this event has kept its planning
        quality.  Conflating the two would make "we rephrased once" look like
        "the Reason model is down", which is the opposite of what happened.
        """

        if self._graph is None:
            return
        try:
            self._graph.emit_event(
                actor="muteki-runtime",
                event_type=EventType.REASON_MODEL_CONTENT_POLICY,
                payload={key: str(value)[:180] for key, value in dict(payload).items()},
            )
        except Exception:  # noqa: BLE001 - observability must not fail the call
            pass

    async def _build_reason_provider(self):
        """Build the Coordinator Reason model separately from Worker engines."""

        def fallback(reason_code: str):
            # A selected Reason model must never fail silently into local
            # strategy logic.  Keep the fallback safe, but make the actual
            # provider choice visible in the same run's audit stream.
            if self._graph is not None:
                self._graph.emit_event(
                    actor="muteki-runtime",
                    event_type=EventType.REASON_MODEL_FALLBACK,
                    payload={"reason_code": str(reason_code)[:100]},
                )
            return self._reason_provider

        reason_id, _ = runtime_selection_from_hints(
            self.run.hints_json,
            fallback_engine_type=self.run.engine_type,
            fallback_model_config_id=self.run.model_config_id,
        )
        if not reason_id:
            return fallback("NO_REASON_MODEL_SELECTED")
        config = await self.session.get(ModelConfig, reason_id)
        if (
            not config
            or not config.enabled
            or config.provider_type not in OPENAI_WIRE_PROVIDER_TYPES
        ):
            return fallback("REASON_MODEL_UNSUPPORTED_OR_DISABLED")
        api_key = decrypt_api_key(config.encrypted_api_key) if config.encrypted_api_key else ""
        if not api_key:
            return fallback("REASON_MODEL_CREDENTIALS_UNAVAILABLE")
        knowledge_context = knowledge_context_service.build(
            self.run,
            self.challenge,
        )
        knowledge_prior = (
            str(knowledge_context.get("formatted_prior") or "")
            if knowledge_context.get("enabled")
            else ""
        )
        # Coordinator Reason receives the full durable Blackboard summary and
        # may use a reasoning model.  The model-config timeout was originally
        # tuned for short Worker turns; reusing a 30s Worker window caused the
        # production Reason request to time out at exactly 30s and be mislabeled
        # as MODEL_UNAVAILABLE.  Keep the user setting as a lower bound source,
        # but give this separate Coordinator boundary a recovery-safe floor.
        reason_timeout_seconds = max(
            120.0,
            float(config.request_timeout_seconds or 30),
        )
        engine = OpenAICompatibleEngine(
            str(config.base_url or ""),
            api_key,
            str(config.model_name or ""),
            timeout=reason_timeout_seconds,
            wire_api=normalize_wire_api(getattr(config, "wire_api", None)),
            action_protocol=str(config.action_protocol or "json_schema"),
            max_output_tokens=int(config.max_output_tokens or 2048),
            temperature=float(config.temperature or 0.0),
            max_retries=int(config.max_retries or 0),
            retry_base_seconds=float(config.retry_base_seconds or 1.0),
            rate_limit_cooldown_seconds=int(config.rate_limit_cooldown_seconds or 60),
        )
        self._reason_model = CoordinatorReasonModel(
            engine,
            model_config_id=str(config.id),
            model_name=str(config.model_name or config.name),
            fallback=self._reason_provider,
            run_id=self._run_id,
            challenge_id=str(self.challenge.id),
            mode=str(getattr(self.challenge, "mode", "ctf") or "ctf"),
            goal=str(getattr(self.challenge, "goal", "") or "") or None,
            knowledge_prior=knowledge_prior,
            challenge_brief=self._declared_surface(),
            usage_recorder=lambda trace: record_muteki_reason_usage(
                SessionLocal,
                run_id=self._run_id,
                model_config_id=str(config.id),
                model_name=str(config.model_name or config.name),
                trace=trace,
            ),
            on_content_policy_retry=self._on_reason_content_policy_retry,
        )
        if self._graph is not None:
            # CAP-02: persist WHAT was recalled, not just how many items came
            # back.  Without the ids and scores, a later experiment review can
            # only guess at the query (which is exactly what the 8ddbdb34
            # analysis had to do); with them, the recall is reproducible from
            # the durable event log alone.
            recalled = [
                {
                    "id": str((entry.get("item") or {}).get("id") or ""),
                    "kind": str((entry.get("item") or {}).get("kind") or ""),
                    "score": round(float(entry.get("score") or 0.0), 4),
                }
                for entry in (knowledge_context.get("results") or [])
                if isinstance(entry, dict)
            ]
            self._graph.emit_event(
                actor="muteki-runtime",
                event_type=EventType.REASON_MODEL_SELECTED,
                payload={
                    "role": "coordinator_reason",
                    "model_config_id": str(config.id),
                    "model": str(config.model_name or config.name)[:120],
                    "knowledge_mode": str(knowledge_context.get("mode") or "off"),
                    "knowledge_enabled": bool(knowledge_context.get("enabled")),
                    "knowledge_result_count": len(knowledge_context.get("results") or []),
                    "knowledge_searched_at": knowledge_context.get("searched_at"),
                    "knowledge_query": str(knowledge_context.get("query") or "")[:2000],
                    "knowledge_top_k": knowledge_context.get("top_k"),
                    "knowledge_items": recalled,
                    "knowledge_item_ids": [entry["id"] for entry in recalled],
                    # The self-produced channel is a separate store with its own
                    # (much smaller) budget.  Recording it separately is the only
                    # way to tell "no prior was recalled" from "a prior was
                    # recalled and it was the wrong one" -- the second is what a
                    # review needs to see, and it is invisible in the item ids.
                    "learned_prior_names": list(knowledge_context.get("learned_prior_names") or []),
                    "learned_prior_chars": int(knowledge_context.get("learned_prior_chars") or 0),
                    "knowledge_signals": dict(knowledge_context.get("metadata") or {}),
                },
            )
        return self._reason_model

    async def _openai_worker_action(
        self,
        job: WorkerJob,
        expected_tool: str,
        arguments: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """Let an OpenAI-compatible Worker refine one declared Tool action.

        The Coordinator has already selected the Intent and tool domain.  The
        model may only return the same declared tool with bounded arguments;
        actual execution remains inside the existing ToolGateway adapter.
        """

        config_id = str((job.environment or {}).get("MUTEKI_MODEL_CONFIG_ID") or "")
        if not config_id:
            return None
        model = self._worker_models.get(config_id)
        if model is None:
            config = await self.session.get(ModelConfig, config_id)
            if (
                not config
                or not config.enabled
                or config.provider_type not in OPENAI_WIRE_PROVIDER_TYPES
            ):
                return None
            api_key = decrypt_api_key(config.encrypted_api_key) if config.encrypted_api_key else ""
            if not api_key:
                return None
            model = OpenAICompatibleEngine(
                str(config.base_url or ""),
                api_key,
                str(config.model_name or ""),
                timeout=float(config.request_timeout_seconds or 30),
                wire_api=normalize_wire_api(getattr(config, "wire_api", None)),
                action_protocol=str(config.action_protocol or "json_schema"),
                max_output_tokens=int(config.max_output_tokens or 2048),
                temperature=float(config.temperature or 0.0),
                max_retries=int(config.max_retries or 0),
                retry_base_seconds=float(config.retry_base_seconds or 1.0),
                rate_limit_cooldown_seconds=int(config.rate_limit_cooldown_seconds or 60),
            )
            self._worker_models[config_id] = model
        knowledge_block = self._worker_knowledge_block(job)
        request_payload: dict[str, Any] = {
            "goal": str(job.goal or "")[:1200],
        }
        if self._declared_surface():
            # This is the pass that actually writes the argument names, so it
            # is the one place a guessed parameter (`warranty_id`) costs the
            # round.  Declared names go before the argument draft for the same
            # truncation reason as in the Chat Completions Worker.
            request_payload["challenge_declared_surface"] = self._declared_surface()
        request_payload["tool_name"] = expected_tool
        request_payload["arguments"] = _safe_worker_arguments(arguments)
        if knowledge_block:
            request_payload["knowledge_prior"] = knowledge_block
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded Muteki Worker. The Coordinator already selected one tool. "
                    "Return exactly one ToolAction JSON. You may not change the tool name, add a new "
                    "tool, claim a finding, or return raw response data. Any knowledge_prior in the "
                    "request is an unverified hypothesis source, not evidence—never report it as a "
                    "finding and always confirm it against the real target."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )[:5000],
            },
        ]
        try:
            action = await model.next_action(messages)
        except Exception:
            return None
        if getattr(action, "type", None) != "tool" or str(getattr(action, "tool_name", "")) != expected_tool:
            return None
        next_arguments = getattr(action, "arguments", None)
        if not isinstance(next_arguments, dict):
            return expected_tool, arguments
        refined = dict(next_arguments)
        for key, value in arguments.items():
            if _sensitive_argument_name(str(key)):
                refined[str(key)] = value
        return expected_tool, refined

    def _worker_knowledge_block(self, job: WorkerJob) -> str:
        """Return one bounded, intent-scoped knowledge prior for a Worker turn.

        The Coordinator Reason pass receives a challenge-scoped prior; Workers
        previously received none, so a Worker could only act on whatever
        technique hint the Reason model happened to write into its intent.  This
        ranks the curated corpus against the Worker's own intent instead.

        Cached per intent: one Intent can refine several Tool actions, and
        re-running the retrieval for each refinement would add cost without
        adding information.
        """

        cache: dict[str, str] = getattr(self, "_worker_prior_cache", None)
        if cache is None:
            cache = {}
            self._worker_prior_cache = cache
        key = str(job.intent_id or job.goal or job.role or "")
        if key in cache:
            return cache[key]

        block = ""
        try:
            payload = dict(job.payload or {})
            context = knowledge_context_service.build_worker_prior(
                self.run,
                self.challenge,
                intent_goal=str(job.goal or ""),
                tool_name=str(payload.get("tool_name") or payload.get("tool") or ""),
                phase=str(getattr(self.run, "current_phase", "") or ""),
            )
            if context.get("enabled"):
                block = str(context.get("prior") or "")
                if self._graph is not None:
                    # Same durable-telemetry shape as runtime.reason.context:
                    # the prior is an input to the Worker, not graph evidence.
                    self._graph.emit_event(
                        actor="muteki-runtime",
                        event_type="runtime.worker.knowledge_prior",
                        payload={
                            "run_id": self._run_id,
                            "worker_id": str(job.worker_id or ""),
                            "intent_id": str(job.intent_id or ""),
                            "tool_name": str(payload.get("tool_name") or payload.get("tool") or "")[:120],
                            "item_ids": list(context.get("item_ids") or [])[:8],
                            "chars": len(block),
                            "searched_at": context.get("searched_at"),
                        },
                    )
        except Exception:
            # A knowledge failure must never remove a Worker's ability to act.
            block = ""
        cache[key] = block
        return block

    def _worker_target(self, worker_backend: str | None = None) -> str | None:
        """The challenge target spelled so a Worker inside the Sandbox can dial it.

        The same trap ``container_reachable_endpoint`` already documents for model
        endpoints applies to the *target*: a range published on the Docker host's
        loopback (``http://127.0.0.1:8383``) is the container's own loopback from
        inside the run Sandbox.  Every probe then comes back ``status_code = 0``,
        the Coordinator reads that as "no new endpoint remains on this route",
        and the Run closes unsolved having never touched the target -- which is
        indistinguishable from a capability failure unless you read the facts.

        Only loopback targets are rewritten, so public arena targets and LAN
        ranges keep the address the platform recorded.
        """

        target = str(self.challenge.target_url or "")
        if not target:
            return None
        backend = worker_backend or resolve_muteki_worker_backend()
        return container_reachable_endpoint(target, backend)

    def _brief_environment(self) -> dict[str, str]:
        """The Challenge's declared surface, encoded for a Worker profile.

        JSON rather than a newline-joined string: one of the declarations can
        itself contain a separator, and a Worker that has to split on a
        delimiter will eventually split inside a value.  Empty when the
        Challenge declares nothing, so an unannotated target is unchanged.
        """

        if not self._declared_surface():
            return {}
        return {
            "MUTEKI_CHALLENGE_BRIEF": json.dumps(
                self._declared_surface(), ensure_ascii=False
            )
        }

    def _declared_surface(self) -> list[str]:
        """The Run's declared surface, tolerating runtime replacements.

        ``MutekiRuntime.__new__`` is the supported construction in the
        integration tests (they skip the DB-bound ``__init__``), so every
        reader goes through this accessor rather than assuming the attribute
        exists.
        """

        return list(getattr(self, "_challenge_brief", None) or [])

    def _worker_round_cap(self) -> int:
        """Upper bound for a single Worker round, in seconds.

        Read from ``app.services.arena_solve`` when available so the arena's own
        number stays the single source of truth, and fall back to the historical
        720 for non-arena callers (benchmark harnesses, unit tests constructed
        through ``__new__``, any future caller that has not opted in).

        A hard-coded 720 was what actually stopped every measured arena run at
        twelve minutes, so the value has to be reachable from the place that
        owns the Run budget.  Imported lazily: ``arena_solve`` pulls in the
        whole services layer, and this module is constructed in tests that must
        stay free of it.
        """

        try:
            from app.services.arena_solve import ARENA_WORKER_ROUND_SECONDS

            return max(1, int(ARENA_WORKER_ROUND_SECONDS))
        except Exception:  # noqa: BLE001 - a missing constant must not break a Run
            return 720

    async def _worker_profiles(self, worker_backend: str) -> list[EngineProfile]:
        """Project the Run's independent Worker selections into Muteki profiles."""

        _, selections = runtime_selection_from_hints(
            self.run.hints_json,
            fallback_engine_type=self.run.engine_type,
            fallback_model_config_id=self.run.model_config_id,
        )
        profiles: list[EngineProfile] = []
        explicit_selections = bool(selections)
        worker_target = self._worker_target(worker_backend)
        for selection in selections:
            environment = {"MUTEKI_TARGET_URL": str(worker_target or "")}
            driver_profile: dict[str, Any] = {}
            if selection.model_config_id:
                config = await self.session.get(ModelConfig, selection.model_config_id)
                if config is None or not config.enabled:
                    continue
                # A retired channel must never execute, however the selection got
                # here (a hand-edited hints_json, an old Run replayed, or a config
                # switched back to it through the update path).  The schema stops
                # new ones being created; this is the guarantee at the boundary
                # that actually launches work.  A disabled config is already
                # skipped above -- this also covers a retired one left enabled.
                if str(config.provider_type or "").strip() in RETIRED_PROVIDER_TYPES:
                    continue
                # Compare the engine the config executes as, not its provider
                # name: local_proxy is a distinct provider running on the
                # openai_compatible engine, while a CLI config must never be
                # routed through another engine's transport.
                if runtime_engine_type(config.provider_type) != selection.engine_type:
                    continue
                if selection.engine_type == "openai_compatible":
                    # Guard the decrypt: an unreadable/absent key must land on the
                    # drop below, not escape as DomainError(API_KEY_MISSING) and
                    # abort the whole run.  Same shape as the CLI branch further
                    # down.
                    api_key = decrypt_api_key(config.encrypted_api_key) if config.encrypted_api_key else ""
                    if not api_key:
                        # Do not silently turn an explicitly selected Worker
                        # into Codex.  The official Coordinator must observe a
                        # failed/unsupported selection instead of changing the
                        # user's engine roster behind their back.
                        continue
                    driver_profile = {
                        "id": selection.engine_id,
                        "name": selection.engine_id,
                        # OpenAI-compatible Workers use Muteki's native
                        # Chat Completions client.  They must not be routed
                        # through Codex EndpointDriver, whose default wire
                        # contract is /responses.  A local_proxy config lands
                        # here too: same protocol, loopback endpoint.
                        "engine": "openai_compatible",
                        "transport": "openai_compatible",
                        # ``protocol`` routes the Worker to the native client;
                        # ``wire_api`` is the dialect that client then speaks.
                        # They are separate on purpose: a Responses-only relay
                        # still executes on the native path, it just needs its
                        # payloads rendered in the Responses schema.
                        "protocol": "chat_completions",
                        "base_url": str(config.base_url or "").strip(),
                        "model": str(config.model_name or config.name).strip(),
                        "wire_api": normalize_wire_api(
                            getattr(config, "wire_api", None), default=CHAT_COMPLETIONS
                        ),
                        "api_key_ref": "env:OPENAI_API_KEY",
                    }
                    environment["OPENAI_API_KEY"] = api_key
                elif selection.engine_type in ("codex_cli", "claude_cli"):
                    engine = "codex" if selection.engine_type == "codex_cli" else "claude"
                    api_key_env = "OPENAI_API_KEY" if engine == "codex" else "ANTHROPIC_API_KEY"
                    model = str(config.model_name or config.name).strip()
                    driver_profile = {
                        "id": selection.engine_id,
                        "name": selection.engine_id,
                        "engine": engine,
                        "transport": selection.engine_type,
                        "protocol": selection.engine_type,
                        "model": model,
                    }
                    if engine == "codex":
                        driver_profile["reasoning_effort"] = str(
                            (config.capabilities_json or {}).get("reasoning_effort") or "medium"
                        ).strip()
                    base_url = container_reachable_endpoint(
                        str(config.base_url or "").strip(), worker_backend
                    )
                    if base_url:
                        driver_profile["base_url"] = base_url
                        if engine == "codex":
                            # Codex only accepts the Responses wire protocol:
                            # `chat` was removed upstream and `chat_completions`
                            # fails config load outright.  Never forward a
                            # legacy stored value to the CLI.
                            driver_profile["wire_api"] = "responses"
                        driver_profile["api_key_ref"] = f"env:{api_key_env}"
                        api_key = (
                            decrypt_api_key(config.encrypted_api_key)
                            if config.encrypted_api_key
                            else ""
                        )
                        if not api_key:
                            # Do not silently drop a selected API Worker.
                            continue
                        environment[api_key_env] = api_key
                        if engine == "codex":
                            # Codex gets its endpoint from the injected
                            # ``-c model_providers.*`` flags built off
                            # driver_profile["base_url"] -- it has no environment
                            # equivalent (contrast the Claude branch below).
                            #
                            # Codex's CLI only speaks the Responses wire (its
                            # binary has no ``chat_completions`` variant at all),
                            # while most relays only serve /chat/completions and
                            # answer /responses with 404 "本站不提供
                            # Responses(reasoning) 协议". When the config opts in,
                            # the endpoint becomes the local Responses proxy --
                            # which codex reaches as a normal Responses server --
                            # and the proxy translates to chat upstream.
                            proxy_base = _codex_proxy_base_url(config, worker_backend)
                            if proxy_base:
                                proxy_token = str((codex_proxy_endpoint() or {}).get("token") or "")
                                if proxy_token:
                                    # The proxy binds 0.0.0.0, so it demands a
                                    # token. codex sends OPENAI_API_KEY as its
                                    # bearer (env_key below), so the proxy token
                                    # has to take that slot or every turn 401s.
                                    environment[api_key_env] = proxy_token
                                # codex appends only ``/responses`` to the base
                                # url it is handed (measured: base ``.../v1``
                                # produced path ``/v1/responses``), while the
                                # proxy serves at ``/v1/responses``. So the
                                # base_url has to carry the ``/v1`` segment or
                                # every turn 404s against the proxy's own router.
                                driver_profile["base_url"] = proxy_base + "/v1"
                        if engine == "claude":
                            # Codex gets its pinned endpoint from injected
                            # `-c model_providers.*` flags; the Claude CLI has no
                            # equivalent, so its endpoint travels in the
                            # environment (this is also what the credential
                            # projection uses, see runtime_env_for_engine).
                            # The CLI appends /v1/messages itself, so the env var
                            # carries the ROOT while driver_profile["base_url"]
                            # keeps the endpoint the health probe appends to.
                            #
                            # A relay that only speaks OpenAI chat cannot serve
                            # the CLI's /v1/messages, which is the whole reason the
                            # local proxy exists. When the config opts in, the
                            # endpoint is replaced by the proxy's -- which the CLI
                            # reaches as a normal Anthropic server -- and the
                            # proxy does the protocol translation upstream.
                            proxy_base = _claude_proxy_base_url(config, worker_backend)
                            environment["ANTHROPIC_BASE_URL"] = proxy_base or anthropic_runtime_base_url(base_url)
                            if proxy_base:
                                proxy_token = str((claude_proxy_endpoint() or {}).get("token") or "")
                                if proxy_token:
                                    # The proxy binds 0.0.0.0, so it demands a
                                    # token; the CLI has to present it or every
                                    # turn 401s.
                                    environment["ANTHROPIC_API_KEY"] = proxy_token
                                    environment["ANTHROPIC_AUTH_TOKEN"] = proxy_token
                                driver_profile["base_url"] = proxy_base + "/v1/messages"
                            # The driver writes a ``--settings`` file on this host,
                            # which a containerised Worker cannot read: the argv is
                            # executed inside the container while the file stays
                            # outside it.  Inside a container the env vars work on
                            # their own (the image ships no ~/.claude/settings.json,
                            # which is the only reason they fail on the host), so the
                            # driver is told which side it is on.
                            driver_profile["containerised"] = (
                                worker_backend in CONTAINER_WORKER_BACKENDS
                            )
                    else:
                        # Local/subscription CLI: use the account store. The
                        # account id honours MUTEKI_<ENGINE>_ACCOUNT_ID, then
                        # MUTEKI_DEFAULT_ACCOUNT_ID, then <engine>-main.
                        account_id = engine_account_id(engine)
                        environment["MUTEKI_CREDENTIAL_ACCOUNT_ID"] = account_id
                        environment[f"MUTEKI_{engine.upper()}_ACCOUNT_ID"] = account_id
                    environment["MUTEKI_WORKER_MODEL"] = model
                elif selection.engine_type == "cursor_cli":
                    # Cursor's headless CLI takes its endpoint from CURSOR_ENDPOINT
                    # (the driver emits `--endpoint <url>`) and its key from
                    # CURSOR_API_KEY. Without an endpoint it authenticates through
                    # the host's own Cursor account instead.
                    model = str(config.model_name or config.name).strip()
                    driver_profile = {
                        "id": selection.engine_id,
                        "name": selection.engine_id,
                        "engine": "cursor",
                        "transport": "cursor_cli",
                        "protocol": "cursor_cli",
                        "model": model,
                    }
                    base_url = container_reachable_endpoint(
                        str(config.base_url or "").strip(), worker_backend
                    )
                    if base_url:
                        # base_url in the profile is what makes driver_for() build
                        # an EndpointDriver, so the endpoint reaches both the
                        # health probe and the real launch.
                        driver_profile["base_url"] = base_url
                        environment["CURSOR_ENDPOINT"] = base_url
                        api_key = (
                            decrypt_api_key(config.encrypted_api_key)
                            if config.encrypted_api_key
                            else ""
                        )
                        if not api_key:
                            # Do not silently drop a selected API Worker: the same
                            # fail-closed rule as the codex/claude branch.
                            continue
                        environment["CURSOR_API_KEY"] = api_key
                        driver_profile["api_key_ref"] = "env:CURSOR_API_KEY"
                    else:
                        account_id = engine_account_id("cursor")
                        environment["MUTEKI_CREDENTIAL_ACCOUNT_ID"] = account_id
                        environment["MUTEKI_CURSOR_ACCOUNT_ID"] = account_id
                    environment["MUTEKI_WORKER_MODEL"] = model
                    # CursorDriver reads its model from this env var on the
                    # subscription path (the endpoint path gets --model injected
                    # by EndpointDriver). Without it cursor falls back to its own
                    # default model, which the operator's relay need not serve.
                    environment["MUTEKI_CURSOR_MODEL"] = model
                environment.update(
                    {
                        "MUTEKI_MODEL_CONFIG_ID": str(config.id),
                        "MUTEKI_MODEL_NAME": str(config.model_name or config.name)[:120],
                    }
                )
            # Every branch above builds a different endpoint credential set, but
            # they must all carry the same declaration of the Challenge's own
            # surface.  Applying it here (rather than in each branch) is what
            # guarantees a comparison between two engines cannot drift on what
            # the target was said to be.
            environment.update(self._brief_environment())
            # ``codex`` is the official vendored driver identity.  Other
            # identities remain explicit so Coordinator scheduling/audit never
            # silently collapses a selected engine into another provider.
            profiles.append(
                EngineProfile(
                    selection.engine_id,
                    environment=environment,
                    worker_class="reasoning" if selection.engine_type == "openai_compatible" else "code",
                    driver_profile=driver_profile,
                )
            )
        if profiles:
            return profiles
        if explicit_selections:
            raise ValueError("MUTEKI_WORKER_SELECTION_UNAVAILABLE")
        return [
            EngineProfile(
                "codex" if worker_backend in {"upstream_local", "upstream_container"} else "gateway-runner",
                environment={
                    "MUTEKI_TARGET_URL": str(self._worker_target(worker_backend) or ""),
                    **self._brief_environment(),
                },
            )
        ]

    async def _record_official_worker_usage(self, job: WorkerJob, result: Any) -> None:
        """Project native Worker usage into the durable RunEvent stream."""

        await record_official_worker_usage(
            SessionLocal,
            run_id=self._run_id,
            worker_id=str(getattr(job, "worker_id", "") or ""),
            intent_id=str(getattr(job, "intent_id", "") or "") or None,
            result=result,
        )

    def _reason_provider(self, snapshot: dict) -> list[dict[str, Any]]:
        metadata = self.challenge.metadata_json or {}
        classification = classify_challenge(metadata, snapshot.get("facts", ()))
        if self._public_credentials is None:
            self._public_credentials = self._recover_public_credentials()
        if self._graph is not None:
            self._graph.emit_event(
                actor="muteki-runtime",
                event_type="runtime.reason.context",
                payload={
                    "classification": classification.classification if classification else None,
                    "public_demo_account_available": bool(self._public_credentials),
                },
            )
        strategy = MutekiStrategyPlanner(
            target_url=str(self._worker_target() or ""),
            metadata=metadata,
            public_credentials=self._public_credentials,
        )
        # The reusable Blackboard strategy owns the normal OODA continuation
        # now: authentication bootstrap, classified web lanes, and the
        # bounded IDOR/path routes all start here. A small generic fallback
        # below remains for incomplete historical fact formats only.
        planned = strategy.plan(snapshot)
        if planned or (classification and classification.classification == "SQLI"):
            return planned
        # Keep this compatibility path only for low-confidence or historical
        # snapshots that the reusable strategy cannot parse. High-confidence
        # classifications must remain wholly owned by MutekiStrategyPlanner.
        if (
            self._public_credentials
            and (classification is None or classification.confidence < 70)
        ):
            facts = "\n".join(
                str(item.get("content") or "")
                for item in snapshot.get("facts", ())
                if isinstance(item, dict)
            )
            folded = facts.casefold()
            target = str(self._worker_target() or "").rstrip("/")
            session_name = "muteki-recon"
            if "request_method=post" not in folded or f"request_url={target}/login" not in folded:
                username, password = self._public_credentials
                return [{
                    "goal": "authenticate using credentials explicitly disclosed by the target",
                    "worker_class": "exploit",
                    "rationale": "Race observed a public demo account; establish one bounded session before probing protected business routes.",
                    "payload": {"tool_name": "http_session_request", "arguments": {
                        "session_name": session_name,
                        "method": "POST",
                        "url": f"{target}/login",
                        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                        "body": f"username={username}&password={password}",
                        "follow_redirects": False,
                    }},
                }]
            for endpoint in _protected_session_endpoints(snapshot, target):
                if f"request_url={endpoint}" in folded:
                    continue
                return [{
                    "goal": f"inspect authenticated endpoint {endpoint}",
                    "worker_class": "exploit",
                    "rationale": "Reuse the established session to inspect the next evidence-backed protected endpoint.",
                    "payload": {"tool_name": "http_session_request", "arguments": {
                        "session_name": session_name,
                        "method": "GET",
                        "url": endpoint,
                        "follow_redirects": False,
                    }},
                }]
        # All remaining classifications use the same graph-driven playbook.
        # A low-confidence GENERIC_WEB result is deliberately treated as
        # reconnaissance only; it must never fall through to SQL actions.
        # A high-confidence non-SQL classification must never fall through to
        # MutekiReason's empty-argument fallback.  That fallback is useful for
        # unit-level graph reasoning, but it cannot satisfy the production
        # Gateway schema.  Use one observed same-origin endpoint as a valid,
        # bounded read-only continuation instead.
        if classification is None or classification.confidence < 70:
            endpoint = _next_observed_endpoint(snapshot, str(self._worker_target() or ""))
            if endpoint:
                classification_name = classification.classification if classification else "GENERIC_WEB"
                tool_name = "http_session_request" if classification_name in {"IDOR", "JWT"} else "http_request"
                arguments: dict[str, Any] = {"method": "GET", "url": endpoint, "follow_redirects": False}
                if tool_name == "http_session_request":
                    arguments["session_name"] = "muteki-recon"
                return [{
                    "goal": f"inspect classified {classification_name.lower()} endpoint {endpoint}",
                    "worker_class": "recon",
                    "rationale": "Continue with one evidence-backed endpoint using a complete Tool Gateway request contract.",
                    "payload": {"tool_name": tool_name, "arguments": arguments, "classification": classification_name},
                }]
        if snapshot.get("facts"):
            return []
        target = str(self._worker_target() or "")
        if not target:
            return []
        return [{
            "goal": "establish target HTTP baseline",
            "worker_class": "gateway",
            "rationale": "Start with one bounded request through the existing Tool Gateway.",
            "payload": {"tool_name": "http_request", "arguments": {"method": "GET", "url": target}},
        }]

    def _recover_public_credentials(self) -> tuple[str, str] | None:
        """Recover only explicitly published demo credentials from this Run.

        The Gateway may keep the full bounded response in an Evidence
        artifact while returning only its model view to the Worker.  This
        fallback reads only this Run's ``outputs`` directory and accepts the
        same explicit public presentation pattern as Race.  It never scans
        source files, challenge metadata, or other workspaces, and the values
        are retained only in runtime memory for one login Intent.
        """

        outputs = Path(self._workspace_path).resolve() / "outputs"
        if not outputs.is_dir():
            return None
        for path in sorted(outputs.glob("*.txt"))[:40]:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
            except OSError:
                continue
            credentials = _public_demo_credentials(content)
            if credentials:
                return credentials
        return None

    async def _disabled_compat_worker(self, job: WorkerJob) -> WorkerOutcome:
        """Fail closed if a production job ever bypasses the container route."""

        if self._graph is not None:
            self._graph.add_dead_end(
                actor=job.worker_id,
                description="LEGACY_WORKER_ROUTE_DISABLED_CONTAINER_ONLY",
            )
        return WorkerOutcome(job.worker_id, "FAILED", result="MUTEKI_CONTAINER_ONLY")

    def _distill(self, graph_path: str, reason: str = "", *, solved: bool = True) -> None:
        """Distil a finished run into the reusable template store.

        Rejection sampling (CTF-Dojo-style): only a Run that actually captured a
        flag becomes a ``case`` template; a Run that ended without one still
        contributes a ``negative`` template recording the routes the swarm ruled
        out, because that is the cheapest way to stop the next Run from
        re-walking them.

        Coordinator Finalize has already emitted the single terminal
        ``RUN_FINISHED`` event by the time Runtime returns.  Distillation is a
        post-run learning side effect, so it must not emit another terminal
        event or append a dead-end to the closed graph.  Failures are reported
        through structured application logging instead.
        """
        import traceback
        from pathlib import Path

        graph_path_text = str(graph_path or "")
        graph_file = Path(graph_path_text)
        if not graph_path_text or not graph_file.is_file():
            # Never create an empty graph just to discover there is nothing to
            # learn: a fabricated empty snapshot would be saved as a junk
            # template for this challenge.
            logger.info(
                "muteki.distill.skipped",
                extra={
                    "run_id": self._run_id,
                    "reason": "graph_missing",
                    "graph_path": graph_path_text,
                    "solved": solved,
                },
            )
            return

        try:
            from app.knowledge.config import knowledge_templates_root
            from app.solver.muteki.upstream_bridge import (
                open_upstream_shared_graph_readonly,
            )
            from muteki.learning.distill import (
                TemplateStore,
                distill_from_events,
                distill_negative_from_events,
            )

            # The Run is already final, so its graph must only ever be read.
            # NOTE: the previous implementation called `SharedGraph(...)`, but
            # `muteki.swarm.shared_graph.SharedGraph` is a typing Protocol — that
            # raised TypeError on every single call and the bare `except` below
            # swallowed it, which is why no template was ever written.
            graph = open_upstream_shared_graph_readonly(
                db_path=graph_path_text, challenge=self.challenge
            )
            store = TemplateStore(root=knowledge_templates_root())
            template = (
                distill_from_events(graph, winner=self._run_id)
                if solved
                else distill_negative_from_events(
                    graph, winner=self._run_id, reason=reason
                )
            )
            if template is None:
                logger.info(
                    "muteki.distill.skipped",
                    extra={
                        "run_id": self._run_id,
                        "reason": reason or "nothing_to_record",
                        "solved": solved,
                    },
                )
                return
            # The store de-duplicates by filename, and its sanitiser collapses a
            # non-ASCII name to ``_`` -- so without a discriminator every
            # Chinese-named challenge overwrote the previous one's template.
            template.name = _disambiguate_template_name(template.name, self.challenge)
            path = store.save(template)
            # A new template changes what recall can return, so the cached BM25
            # indexes must not outlive it.
            clear_retriever_cache()
            logger.info(
                "muteki.distill.completed",
                extra={
                    "run_id": self._run_id,
                    "template_name": template.name,
                    "template_category": template.category,
                    "template_kind": getattr(template, "kind", "") or "",
                    "template_keywords": list(template.keywords[:8]),
                    "template_path": str(path),
                    "reason": reason,
                },
            )
        except Exception as error:
            # Never silent: an unrecorded distillation means the self-improving
            # loop has stopped working, which must be visible in the logs.
            logger.warning(
                "muteki.distill.failed",
                extra={
                    "run_id": self._run_id,
                    "reason": reason,
                    "solved": solved,
                    "error_type": type(error).__name__,
                    "error": str(error)[:300],
                    "graph_path": graph_path_text,
                    "traceback": traceback.format_exc()[:1200],
                },
            )


def _sensitive_argument_name(name: str) -> bool:
    folded = name.casefold()
    return any(token in folded for token in ("password", "passwd", "secret", "token", "cookie", "authorization", "api_key"))


def _safe_worker_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): "<redacted>" if _sensitive_argument_name(str(key)) else value
        for key, value in arguments.items()
    }


__all__ = ["MutekiRuntime"]


def _protected_session_endpoints(snapshot: dict[str, Any], target: str) -> list[str]:
    """Return bounded, same-origin routes that Race marked as protected."""

    from urllib.parse import urlparse

    origin = urlparse(target).netloc.casefold()
    candidates: list[str] = []
    for item in snapshot.get("facts", ()):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            continue
        if value.get("type") == "ENDPOINT_OBSERVED":
            entries = [value]
        elif value.get("type") == "ENDPOINTS_DISCOVERED":
            entries = [entry for entry in value.get("endpoints", ()) if isinstance(entry, dict)]
        else:
            entries = []
        for entry in entries:
            endpoint = str(entry.get("endpoint") or "")
            if not endpoint or urlparse(endpoint).netloc.casefold() != origin:
                continue
            path = urlparse(endpoint).path.rstrip("/").casefold()
            if path in {"", "/login"}:
                continue
            status = int(entry.get("status_code") or 0)
            if bool(entry.get("auth_required")) or status in {301, 302, 303, 307, 308, 401, 403}:
                if endpoint not in candidates:
                    candidates.append(endpoint)
    return candidates[:8]


def _next_observed_endpoint(snapshot: dict[str, Any], target: str) -> str | None:
    """Return one unrequested, same-origin endpoint from Race facts."""

    from urllib.parse import urlparse

    origin = urlparse(target).netloc.casefold()
    requested = {
        value.casefold()
        for item in snapshot.get("facts", ())
        if isinstance(item, dict)
        for value in re.findall(r"request_url=([^;\s]+)", str(item.get("content") or ""), re.IGNORECASE)
    }
    candidates: list[str] = []
    for item in snapshot.get("facts", ()):
        if not isinstance(item, dict):
            continue
        try:
            value = json.loads(str(item.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        entries = [value] if value.get("type") == "ENDPOINT_OBSERVED" else [entry for entry in value.get("endpoints", ()) if isinstance(entry, dict)] if value.get("type") == "ENDPOINTS_DISCOVERED" else []
        for entry in entries:
            endpoint = str(entry.get("endpoint") or "")
            if not endpoint or urlparse(endpoint).netloc.casefold() != origin:
                continue
            if endpoint.casefold() in requested or endpoint not in candidates:
                if endpoint.casefold() not in requested and endpoint not in candidates:
                    candidates.append(endpoint)
    return candidates[0] if candidates else None
