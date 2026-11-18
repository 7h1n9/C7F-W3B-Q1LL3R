"""Endpoint handling for the CLI providers (codex / claude / cursor).

Three field bugs motivated this file:

* Claude's base_url was pasted as the full ``.../v1/messages`` endpoint (which is
  what an Anthropic-compatible relay publishes) and the code appended the path
  again -> HTTP 404 "Unknown path: /v1/messages/v1/messages".
* The settings probe gave a remote reasoning model 3s to answer, so a healthy
  endpoint was reported as ``CODEX_API_PROBE_TIMEOUT``.
* ``cursor_cli`` refused an endpoint outright even though Cursor's headless CLI
  accepts ``--endpoint <url>``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.models import (
    CODEX_API_PROBE_TIMEOUT_SECONDS,
    MODEL_TEST_TOTAL_BUDGET_SECONDS,
    _anthropic_messages_url,
)
from app.engines.wire import resolve_endpoint, strip_endpoint_path
from app.schemas.model_config import (
    CLI_PROVIDER_TYPES,
    ENDPOINT_CAPABLE_CLI_PROVIDERS,
    ModelConfigWrite,
)
from app.solver.muteki.runtime.muteki_runtime import (
    MutekiRuntime,
    anthropic_runtime_base_url,
    container_reachable_endpoint,
)
from muteki.solver.cli_driver import CursorDriver
from muteki.solver.worker_profiles import profile_uses_endpoint

# ── the Anthropic path must be appended exactly once ────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # The spelling the relay's own docs publish.
        ("http://127.0.0.1:8787/v1/messages", "http://127.0.0.1:8787/v1/messages"),
        ("http://127.0.0.1:8787/v1/messages/", "http://127.0.0.1:8787/v1/messages"),
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("http://127.0.0.1:8787", "http://127.0.0.1:8787/v1/messages"),
        # An OpenAI-wire endpoint pasted as the base_url.  A relay that serves
        # both wires is common, and the old append produced
        # ``.../v1/chat/completions/v1/messages`` -- a path no relay serves,
        # which reads as a broken config (HTTP 404) rather than a suffix clash.
        (
            "https://sub.gavinchen.top/v1/chat/completions",
            "https://sub.gavinchen.top/v1/messages",
        ),
        ("https://relay.example/v1/responses", "https://relay.example/v1/messages"),
        ("https://relay.example/v1/completions", "https://relay.example/v1/messages"),
        ("https://relay.example/v1/chat/completions/", "https://relay.example/v1/messages"),
    ],
)
def test_anthropic_endpoint_accepts_both_spellings(given: str, expected: str) -> None:
    assert _anthropic_messages_url(given) == expected


def test_the_doubled_path_can_no_longer_occur() -> None:
    # The exact string the failing probe produced.
    url = _anthropic_messages_url("http://127.0.0.1:8787/v1/messages")
    assert url.count("/v1/messages") == 1


def test_an_openai_base_url_never_yields_a_doubled_path() -> None:
    """The regression the settings page hit: AP stored the chat endpoint and
    the probe asked for ``/v1/chat/completions/v1/messages``."""
    url = _anthropic_messages_url("https://sub.gavinchen.top/v1/chat/completions")
    assert url == "https://sub.gavinchen.top/v1/messages"
    assert "/chat/completions/v1" not in url


def test_strip_endpoint_path_shares_one_definition_with_resolve_endpoint() -> None:
    """Both directions must agree on what counts as a pasted endpoint.

    A pasted endpoint suffix is removed; a bare root is left alone.  Only paths
    in the shared table count, so a relay base that happens to end in ``/v1``
    keeps it.
    """
    for base in (
        "https://relay.example/v1/chat/completions",
        "https://relay.example/v1/messages",
        "https://relay.example/v1/responses",
        "https://relay.example/v1/completions",
    ):
        assert strip_endpoint_path(base) == "https://relay.example/v1", base
    for root in ("https://relay.example", "https://relay.example/v1"):
        assert strip_endpoint_path(root) == root
    # resolve_endpoint builds on it, so a pasted suffix can never double either.
    assert (
        resolve_endpoint("https://relay.example/v1/chat/completions")
        == "https://relay.example/v1/chat/completions"
    )
    assert (
        resolve_endpoint("https://relay.example/v1/responses", "responses")
        == "https://relay.example/v1/responses"
    )


# ── probe budget must allow one real round trip ─────────────────────────────


def test_the_endpoint_budget_allows_a_slow_reasoning_first_byte() -> None:
    # A reasoning model on a relay routinely needs >3s to first byte; the old
    # 3s budget turned that into a false CODEX_API_PROBE_TIMEOUT.
    assert CODEX_API_PROBE_TIMEOUT_SECONDS >= 10
    assert CODEX_API_PROBE_TIMEOUT_SECONDS < MODEL_TEST_TOTAL_BUDGET_SECONDS


# ── endpooint-capable set ───────────────────────────────────────────────────


def test_every_cli_provider_can_pin_an_endpoint() -> None:
    assert ENDPOINT_CAPABLE_CLI_PROVIDERS == CLI_PROVIDER_TYPES
    # cursor_cli is still in both sets -- the driver and its endpoint flag are
    # untouched -- but the channel can no longer be entered, so there is no
    # configuration that could exercise it.
    assert "cursor_cli" in ENDPOINT_CAPABLE_CLI_PROVIDERS
    with pytest.raises(ValidationError, match="no longer offered"):
        ModelConfigWrite(
            name="Cursor 网关",
            provider_type="cursor_cli",
            base_url="https://gateway.example.com/v1",
            model_name="cursor-default",
            api_key="sk-cursor",
            roles=["worker"],
        )


def test_the_cursor_driver_still_exists_for_a_future_channel() -> None:
    """Retirement removed the *channel*, not the vendored driver.

    Keeping the driver means re-enabling Cursor later is a schema + frontend
    change, not a rewrite; these assertions pin that the capability is intact.
    """

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("CURSOR_ENDPOINT", "https://gateway.example.com/v1")
        argv = CursorDriver().build_execute("probe", None)
        assert "--endpoint" in argv
    finally:
        monkeypatch.undo()


# ── the driver must actually forward the endpoint ───────────────────────────


def test_cursor_driver_forwards_the_endpoint_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_ENDPOINT", "https://gateway.example.com/v1")
    argv = CursorDriver().build_execute("probe the target", None)
    assert "--endpoint" in argv
    assert argv[argv.index("--endpoint") + 1] == "https://gateway.example.com/v1"
    # The prompt must stay last: cursor takes it as a trailing positional.
    assert argv[-1] == "probe the target"


def test_cursor_driver_omits_the_flag_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_ENDPOINT", raising=False)
    argv = CursorDriver().build_execute("probe", None)
    assert "--endpoint" not in argv


def test_cursor_resume_also_carries_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_ENDPOINT", "https://gateway.example.com/v1")
    argv = CursorDriver().build_resume("probe", "chat-1")
    assert "--endpoint" in argv
    assert argv[-1] == "probe"


def test_an_endpoint_makes_the_profile_use_the_endpoint_driver() -> None:
    # driver_for() builds an EndpointDriver iff the profile carries base_url.
    assert profile_uses_endpoint({"base_url": "https://gw.example.com/v1"}) is True
    assert profile_uses_endpoint({"base_url": ""}) is False
    assert profile_uses_endpoint({}) is False


# ── binary resolution ───────────────────────────────────────────────────────


def test_known_good_locations_expand_environment_variables() -> None:
    # Cursor's Windows-native installer drops a .cmd shim under
    # %LOCALAPPDATA%\\cursor-agent and only appends that directory to the *user*
    # PATH -- a process that was already running when the install happened never
    # sees it, and expanduser alone leaves %VAR% untouched.
    entry = "%LOCALAPPDATA%/cursor-agent/cursor-agent.cmd"
    expanded = os.path.expanduser(os.path.expandvars(entry))
    assert "%LOCALAPPDATA%" not in expanded
    if sys.platform == "win32":
        assert expanded.lower().startswith(os.environ["LOCALAPPDATA"].lower())


def test_cursor_has_a_windows_known_good_entry() -> None:
    from muteki.solver.cli_driver import _KNOWN_GOOD

    cursor_entries = _KNOWN_GOOD["cursor"]
    assert any("cursor-agent.cmd" in entry for entry in cursor_entries)
    # Every entry must be environment-expandable, or the Windows one is inert.
    for entry in cursor_entries:
        assert os.path.expanduser(os.path.expandvars(entry))


def test_the_cursor_engine_resolves_to_a_cursor_agent_binary() -> None:
    from muteki.solver.cli_driver import _BIN_NAME, resolve_engine_bin

    assert _BIN_NAME["cursor"] == "cursor-agent"
    # Whatever the host has (or lacks), the resolved basename must be the
    # cursor-agent CLI, never the GUI `cursor` launcher.
    resolved = resolve_engine_bin("cursor")
    assert "cursor" in os.path.basename(resolved).lower()


# ── loopback model endpoints must survive the container boundary ────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # The exact endpoint that killed run 32514c76: inside the Worker
        # Sandbox 127.0.0.1 is the container, so the host's proxy is invisible.
        ("http://127.0.0.1:8787/v1", "http://host.docker.internal:8787/v1"),
        ("http://127.0.0.1:8787/v1/messages", "http://host.docker.internal:8787/v1/messages"),
        ("http://localhost:8787/v1", "http://host.docker.internal:8787/v1"),
        ("https://127.0.0.1:8443/v1", "https://host.docker.internal:8443/v1"),
        # No port: the host alias still replaces the loopback name.
        ("http://127.0.0.1/v1", "http://host.docker.internal/v1"),
    ],
)
def test_a_loopback_model_endpoint_is_rewritten_for_container_workers(
    given: str, expected: str
) -> None:
    assert container_reachable_endpoint(given, "upstream_container") == expected


@pytest.mark.parametrize(
    "given",
    [
        # A public relay is reachable as-is from inside the container.
        "https://api.gavinchen.top/v1",
        # A LAN address is reached directly by a bridge container; rewriting it
        # to the host alias would turn a working endpoint into a broken one.
        "http://192.168.1.10:8000/v1",
        "http://10.0.0.5:8000/v1",
        # A compose service name only resolves inside the Docker network.
        "http://muteki-web:8000/v1",
    ],
)
def test_non_loopback_model_endpoints_are_left_alone(given: str) -> None:
    assert container_reachable_endpoint(given, "upstream_container") == given


def test_a_local_worker_backend_keeps_loopback_as_is() -> None:
    # A host-local Worker dials 127.0.0.1 directly, so rewriting would break it.
    given = "http://127.0.0.1:8787/v1"
    assert container_reachable_endpoint(given, "upstream_local") == given


def test_the_container_target_host_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A backend deployed inside the same compose network names the service.
    monkeypatch.setenv("MUTEKI_CONTAINER_TARGET_HOST", "muteki-web")
    assert (
        container_reachable_endpoint("http://127.0.0.1:8787/v1", "upstream_container")
        == "http://muteki-web:8787/v1"
    )


def test_malformed_endpoints_are_returned_unchanged() -> None:
    for given in ("", "not-a-url", "ftp://127.0.0.1/v1", "127.0.0.1:8787"):
        assert container_reachable_endpoint(given, "upstream_container") == given


# ── the challenge target needs the same rewrite as the model endpoint ───────


def _runtime_with_target(target: object):
    """A MutekiRuntime stub exposing only what ``_worker_target`` reads."""

    runtime = object.__new__(MutekiRuntime)
    runtime.challenge = SimpleNamespace(target_url=target)
    return runtime


def test_a_loopback_challenge_target_is_rewritten_for_container_workers() -> None:
    # The exact address that made run 12e8c375 report ``status_code = 0`` on
    # every probe: the range is published on the host loopback, so inside the
    # Worker Sandbox it addressed the container itself.
    runtime = _runtime_with_target("http://127.0.0.1:8383")
    assert (
        MutekiRuntime._worker_target(runtime, "upstream_container")
        == "http://host.docker.internal:8383"
    )


def test_a_public_challenge_target_is_left_alone() -> None:
    given = "http://66a5975ddb59251b989f04be.tcp-ctf2.dasctf.com:9999"
    runtime = _runtime_with_target(given)
    assert MutekiRuntime._worker_target(runtime, "upstream_container") == given


def test_a_host_local_worker_keeps_the_loopback_target() -> None:
    runtime = _runtime_with_target("http://127.0.0.1:8383")
    assert (
        MutekiRuntime._worker_target(runtime, "upstream_local")
        == "http://127.0.0.1:8383"
    )


def test_a_challengeless_run_has_no_worker_target() -> None:
    for empty in (None, ""):
        runtime = _runtime_with_target(empty)
        assert MutekiRuntime._worker_target(runtime, "upstream_container") is None


def test_the_worker_target_host_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUTEKI_CONTAINER_TARGET_HOST", "range-gateway")
    runtime = _runtime_with_target("http://127.0.0.1:8383")
    assert (
        MutekiRuntime._worker_target(runtime, "upstream_container")
        == "http://range-gateway:8383"
    )


# ── the Claude CLI appends /v1/messages itself ──────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # The relay publishes this full endpoint; the CLI would double the path.
        ("http://127.0.0.1:8787/v1/messages", "http://127.0.0.1:8787"),
        ("http://127.0.0.1:8787/v1", "http://127.0.0.1:8787"),
        ("https://api.anthropic.com", "https://api.anthropic.com"),
        ("http://127.0.0.1:8787/", "http://127.0.0.1:8787"),
    ],
)
def test_anthropic_runtime_base_url_is_the_root(stored: str, expected: str) -> None:
    assert anthropic_runtime_base_url(stored) == expected


def test_the_claude_cli_never_receives_a_doubled_messages_path() -> None:
    # ANTHROPIC_BASE_URL is the root; the CLI appends /v1/messages once.
    root = anthropic_runtime_base_url("http://host.docker.internal:8787/v1/messages")
    assert root == "http://host.docker.internal:8787"
    assert f"{root}/v1/messages".count("/v1/messages") == 1


# ── endpoint drivers must still pin the configured model ────────────────────


def test_an_endpoint_pinned_claude_still_gets_its_model() -> None:
    # Without --model the CLI falls back to its own default model name, which an
    # operator's relay usually does not serve; the turn then fails with a
    # misleading "selected model may not exist" instead of an endpoint error.
    from muteki.solver.cli_driver import driver_for

    driver = driver_for(
        {
            "engine": "claude",
            "transport": "claude_cli",
            "protocol": "claude_cli",
            "model": "deepseek-v4.1-flash",
            "base_url": "http://host.docker.internal:8787",
        }
    )
    argv = driver.build_execute("probe", None, web_access=False, kb_access=False, stream=False)
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "deepseek-v4.1-flash"
    assert argv[-1] == "probe"


def test_an_endpoint_pinned_cursor_gets_its_model() -> None:
    from muteki.solver.cli_driver import driver_for

    driver = driver_for(
        {
            "engine": "cursor",
            "transport": "cursor_cli",
            "protocol": "cursor_cli",
            "model": "hy4",
            "base_url": "http://host.docker.internal:8787/v1",
        }
    )
    # Cursor cannot honour web_access=False (no offline isolation), so probe the
    # way a web-enabled run would.
    argv = driver.build_execute("probe", None, web_access=True, kb_access=False, stream=False)
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "hy4"


def test_codex_carries_its_model_in_the_provider_config_not_a_flag() -> None:
    from muteki.solver.cli_driver import driver_for

    driver = driver_for(
        {
            "engine": "codex",
            "transport": "codex_cli",
            "protocol": "codex_cli",
            "model": "deepseek-v4.1-flash",
            "base_url": "http://host.docker.internal:8787/v1",
            "wire_api": "responses",
        }
    )
    argv = driver.build_execute("probe", None, web_access=False, kb_access=False, stream=False)
    assert "--model" not in argv
    assert "model=deepseek-v4.1-flash" in argv


def test_codex_provider_config_is_pinned_to_the_responses_wire_api() -> None:
    """A legacy `chat_completions` value must never reach the CLI.

    Codex aborts at config load with ``unknown variant `chat_completions`,
    expected `responses` `` before sending a request, and `chat` was removed
    upstream entirely, so the driver normalises whatever the profile holds.
    """
    from muteki.solver.cli_driver import driver_for

    driver = driver_for(
        {
            "engine": "codex",
            "transport": "codex_cli",
            "protocol": "codex_cli",
            "model": "deepseek-v4.1-flash",
            "base_url": "http://host.docker.internal:8787/v1",
            "wire_api": "chat_completions",
        }
    )
    argv = driver.build_execute("probe", None, web_access=False, kb_access=False, stream=False)
    assert "model_providers.muteki.wire_api=responses" in argv
    assert not any("chat_completions" in arg for arg in argv)
