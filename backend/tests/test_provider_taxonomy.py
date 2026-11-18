"""Provider taxonomy regression tests.

Covers the five-provider contract added for the local-proxy / Claude / Cursor
work: the loopback endpoint rule, the provider-level validation matrix, the
provider -> engine mapping, and the Worker profile projection each engine
branch produces.  Everything here is pure: no database, no network, no CLI
binary is ever executed.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.exceptions import DomainError
from app.core.net import is_loopback_host, is_loopback_url, require_loopback_url
from app.schemas.model_config import (
    CLI_PROVIDER_TYPES,
    ENDPOINT_CAPABLE_CLI_PROVIDERS,
    OPENAI_WIRE_PROVIDER_TYPES,
    RETIRED_PROVIDER_TYPES,
    RUNTIME_ENGINE_TYPE_BY_PROVIDER,
    WORKER_ENGINE_ID_PREFIX,
    WORKER_ENGINE_TYPES,
    WORKER_PROVIDER_TYPES,
    ModelConfigWrite,
    normalize_worker_engine_type,
    runtime_engine_type,
)
from app.services.crypto import encrypt_api_key
from app.solver.muteki.runtime.configuration import (
    WorkerEngineSelection,
    normalize_worker_engines,
    write_runtime_selection,
)
from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime
from app.solver.muteki.workers import EngineProfile

# cursor_cli was retired (see app.schemas.model_config.RETIRED_PROVIDER_TYPES);
# the channel can no longer be entered, so it is not part of the offered set.
CLI_WORKER_ENGINES = ("codex_cli", "claude_cli")
ALL_WORKER_ENGINES = ("openai_compatible", "codex_cli", "claude_cli", "cursor_cli")

LOOPBACK_ACCEPTED = [
    "http://127.0.0.1:8080/v1",
    "https://127.0.0.1",
    "http://127.5.5.5/v1",
    "http://localhost:1234/v1",
    "https://localhost",
]
# 0.0.0.0 and the RFC1918 ranges are rejected on purpose: the requirement is
# loopback, not "reachable from my LAN".  127.0.0.1.evil.com is the case a naive
# prefix match gets wrong -- it is a name, and names are never resolved here.
LOOPBACK_REJECTED = [
    "http://0.0.0.0:8080",
    "http://192.168.1.10:8080",
    "http://10.0.0.1",
    "http://172.16.0.9",
    "http://8.8.8.8",
    "http://example.com",
    "http://127.0.0.1.evil.com",
    "http://localhost.evil.com",
    "ftp://127.0.0.1",
]


# ── loopback endpoint rule ──────────────────────────────────────────────────


@pytest.mark.parametrize("url", LOOPBACK_ACCEPTED)
def test_loopback_endpoint_accepts_literal_loopback_and_localhost(url: str) -> None:
    assert is_loopback_url(url) is True
    assert require_loopback_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", LOOPBACK_REJECTED)
def test_loopback_endpoint_rejects_lan_public_and_lookalike_hosts(url: str) -> None:
    assert is_loopback_url(url) is False
    with pytest.raises(ValueError, match="loopback"):
        require_loopback_url(url)


def test_loopback_check_never_resolves_a_hostname() -> None:
    # example.com resolves fine, which is exactly why it must not qualify: DNS at
    # validation time is both a network dependency and a rebinding window.
    assert is_loopback_host("example.com") is False
    assert is_loopback_host("LOCALHOST") is True
    assert is_loopback_host("[::1]") is True
    assert is_loopback_host("") is False


def test_loopback_rejection_names_the_offending_host() -> None:
    with pytest.raises(ValueError) as captured:
        require_loopback_url("http://192.168.1.10:8080/v1")
    assert "192.168.1.10" in str(captured.value)


def test_an_empty_endpoint_is_reported_as_missing_not_as_an_illegal_host() -> None:
    with pytest.raises(ValueError, match="required"):
        require_loopback_url("")


# ── provider -> engine mapping ──────────────────────────────────────────────


def test_local_proxy_is_a_provider_that_runs_on_the_openai_wire_engine() -> None:
    assert runtime_engine_type("local_proxy") == "openai_compatible"
    assert RUNTIME_ENGINE_TYPE_BY_PROVIDER["local_proxy"] == "openai_compatible"
    # It is deliberately NOT a runtime engine identity: the Coordinator,
    # hints_json selection and WorkerPool must not grow a fifth engine for what
    # is the same execution path.
    assert "local_proxy" not in WORKER_ENGINE_TYPES
    assert "local_proxy" in WORKER_PROVIDER_TYPES
    assert "local_proxy" in OPENAI_WIRE_PROVIDER_TYPES


@pytest.mark.parametrize(
    ("provider", "engine"),
    [
        ("openai_compatible", "openai_compatible"),
        ("local_proxy", "openai_compatible"),
        ("codex_cli", "codex_cli"),
        ("claude_cli", "claude_cli"),
        ("cursor_cli", "cursor_cli"),
    ],
)
def test_every_provider_maps_onto_exactly_one_known_engine(provider: str, engine: str) -> None:
    assert runtime_engine_type(provider) == engine
    assert engine in WORKER_ENGINE_TYPES


def test_an_unknown_provider_falls_back_to_the_openai_wire_engine() -> None:
    assert runtime_engine_type("brand_new") == "openai_compatible"
    assert runtime_engine_type("") == "openai_compatible"


@pytest.mark.parametrize(
    ("spelling", "engine"),
    [
        ("codex", "codex_cli"),
        ("codex-sdk", "codex_cli"),
        ("codex-cli", "codex_cli"),
        ("CLAUDE", "claude_cli"),
        ("claude-code", "claude_cli"),
        ("cursor", "cursor_cli"),
        ("cursor-agent", "cursor_cli"),
        ("openai-compatible", "openai_compatible"),
        # A provider name leaking in as an engine name resolves to the engine it
        # actually executes through, so the mapping keeps one definition.
        ("local_proxy", "openai_compatible"),
        ("local-proxy", "openai_compatible"),
    ],
)
def test_engine_aliases_normalize_to_the_canonical_name(spelling: str, engine: str) -> None:
    assert normalize_worker_engine_type(spelling) == engine


def test_unrecognized_and_mock_engines_resolve_to_nothing() -> None:
    assert normalize_worker_engine_type("mock") == ""
    assert normalize_worker_engine_type("gpt-5") == ""
    assert normalize_worker_engine_type(None) == ""


@pytest.mark.parametrize("engine", ALL_WORKER_ENGINES)
def test_each_engine_owns_a_distinct_identity_prefix(engine: str) -> None:
    selection = WorkerEngineSelection(engine, "cfg-1")
    assert selection.engine_type == engine
    assert selection.engine_id == f"{WORKER_ENGINE_ID_PREFIX[engine]}:cfg-1"


def test_engine_identity_prefixes_never_collide() -> None:
    assert len(set(WORKER_ENGINE_ID_PREFIX.values())) == len(WORKER_ENGINE_ID_PREFIX)
    assert set(WORKER_ENGINE_ID_PREFIX) == set(WORKER_ENGINE_TYPES)


@pytest.mark.parametrize("engine", ALL_WORKER_ENGINES)
def test_a_worker_selection_without_a_config_is_rejected(engine: str) -> None:
    with pytest.raises(ValueError, match="model_config_id"):
        WorkerEngineSelection(engine)


def test_normalize_worker_engines_drops_mock_unknown_and_unbound() -> None:
    selections = normalize_worker_engines(
        [
            {"engine_type": "mock", "model_config_id": "m"},
            {"engine_type": "nonsense", "model_config_id": "n"},
            {"engine_type": "claude_cli", "model_config_id": ""},
            {"engine_type": "claude_cli", "model_config_id": "cl-main"},
            {"engine_type": "claude-cli", "model_config_id": "cl-main"},
            {"engine_type": "cursor_cli", "model_config_id": "cu-main"},
        ]
    )
    assert [item.engine_id for item in selections] == ["claude-cli:cl-main", "cursor-cli:cu-main"]


# ── provider-level validation matrix ────────────────────────────────────────


def test_local_proxy_requires_a_loopback_endpoint() -> None:
    config = ModelConfigWrite(
        name="本机反代",
        provider_type="local_proxy",
        base_url="http://127.0.0.1:8000/v1",
        model_name="deepseek-chat",
        api_key="placeholder",
        roles=["worker"],
    )
    assert config.base_url is not None
    # A local proxy speaks the OpenAI wire, so it carries a dialect like any
    # other OpenAI-wire provider -- defaulting to Chat Completions, which is
    # what it served before the dialect became selectable.
    assert config.wire_api == "chat_completions"

    for host in ("http://192.168.1.10:8000/v1", "http://0.0.0.0:8000/v1", "http://api.example.com/v1"):
        with pytest.raises(ValidationError, match="loopback"):
            ModelConfigWrite(
                name="越界反代",
                provider_type="local_proxy",
                base_url=host,
                model_name="deepseek-chat",
                api_key="placeholder",
                roles=["worker"],
            )


def test_openai_wire_providers_require_an_endpoint() -> None:
    for provider in sorted(OPENAI_WIRE_PROVIDER_TYPES):
        with pytest.raises(ValidationError, match="base_url"):
            ModelConfigWrite(name="缺 URL", provider_type=provider, model_name="m", roles=["worker"])


def test_the_api_key_rule_for_wire_providers_lives_in_the_api_layer() -> None:
    # The schema deliberately does not require api_key: the model-config API owns
    # that rule, because it is the layer that encrypts the key and can therefore
    # decide whether the row stores one at all.
    from app.api.v1.models import _stores_api_key

    wire = {
        provider: _stores_api_key(_wire_payload(provider))
        for provider in sorted(OPENAI_WIRE_PROVIDER_TYPES)
    }
    cli = {
        provider: _stores_api_key(
            ModelConfigWrite(name="n", provider_type=provider, model_name="m", roles=["worker"])
        )
        for provider in sorted(CLI_PROVIDER_TYPES - RETIRED_PROVIDER_TYPES)
    }
    assert wire == {"local_proxy": True, "openai_compatible": True}
    assert cli == {"claude_cli": False, "codex_cli": False}

    # ... and because the API stores a key for both wire providers, a blank one
    # cannot be saved.
    with pytest.raises(DomainError) as captured:
        encrypt_api_key("   ")
    assert captured.value.code == "API_KEY_REQUIRED"


def _wire_payload(provider: str) -> ModelConfigWrite:
    return ModelConfigWrite(
        name="n",
        provider_type=provider,
        base_url="http://127.0.0.1:8000/v1" if provider == "local_proxy" else "https://api.example.com/v1",
        model_name="m",
        roles=["worker"],
    )


@pytest.mark.parametrize("provider", CLI_WORKER_ENGINES)
def test_cli_providers_are_worker_only(provider: str) -> None:
    assert provider in CLI_PROVIDER_TYPES
    with pytest.raises(ValidationError, match="Worker-only"):
        ModelConfigWrite(name="错误角色", provider_type=provider, model_name="m", roles=["coordinator_reason"])


def test_cli_providers_accept_the_bare_worker_role_on_a_local_login() -> None:
    for provider in CLI_WORKER_ENGINES:
        config = ModelConfigWrite(name="本机 CLI", provider_type=provider, model_name="m", roles=["worker"])
        assert config.base_url is None
        assert config.api_key is None


def test_claude_cli_may_pin_an_endpoint_but_then_needs_a_key() -> None:
    assert "claude_cli" in ENDPOINT_CAPABLE_CLI_PROVIDERS
    with pytest.raises(ValidationError, match="api_key"):
        ModelConfigWrite(
            name="Claude 端点无 Key",
            provider_type="claude_cli",
            base_url="https://api.anthropic.com",
            model_name="claude-sonnet-4-5",
            roles=["worker"],
        )
    config = ModelConfigWrite(
        name="Claude 端点",
        provider_type="claude_cli",
        base_url="https://api.anthropic.com",
        model_name="claude-sonnet-4-5",
        api_key="sk-ant",
        roles=["worker"],
    )
    assert config.base_url is not None
    assert config.wire_api is None


def test_codex_cli_still_defaults_to_the_responses_wire_api() -> None:
    config = ModelConfigWrite(
        name="Codex API",
        provider_type="codex_cli",
        base_url="https://api.openai.com/v1",
        model_name="gpt-5-codex",
        api_key="sk-test",
        roles=["worker"],
    )
    assert config.wire_api == "responses"
    assert config.reasoning_effort == "medium"


def test_the_cursor_channel_is_retired() -> None:
    """Replaces the old "Cursor can pin an endpoint" contract.

    The channel was withdrawn because the headless Cursor CLI speaks a private
    ConnectRPC API that an OpenAI-compatible relay cannot serve, so no endpoint
    configuration could ever pass its health probe.  Entering the channel is
    refused; rows already stored stay readable and editable (see
    test_retired_provider_channels.py for the full contract).
    """

    with pytest.raises(ValidationError, match="no longer offered"):
        ModelConfigWrite(
            name="Cursor 网关",
            provider_type="cursor_cli",
            base_url="https://gateway.example.com/v1",
            model_name="m",
            api_key="sk-test",
            roles=["worker"],
        )


# ── Worker profile projection ───────────────────────────────────────────────


class _FakeSession:
    """Minimal stand-in for AsyncSession.get(ModelConfig, id)."""

    def __init__(self, configs: list[SimpleNamespace]) -> None:
        self._by_id = {config.id: config for config in configs}

    async def get(self, _model: object, identity: str) -> object | None:
        return self._by_id.get(identity)


def _config(**overrides: object) -> SimpleNamespace:
    base = {
        "id": "cfg",
        "name": "cfg",
        "model_name": "model-x",
        "provider_type": "claude_cli",
        "enabled": True,
        "base_url": None,
        "encrypted_api_key": None,
        "capabilities_json": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _workspace() -> str:
    """A throwaway workspace path; _worker_profiles never writes to it."""

    root = Path(tempfile.gettempdir()) / "muteki-provider-taxonomy"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _profiles(
    selections: list[WorkerEngineSelection],
    configs: list[SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
    *,
    hints: dict | None = None,
    engine_type: str = "openai_compatible",
    model_config_id: str | None = None,
) -> list[EngineProfile]:
    for name in (
        "MUTEKI_DEFAULT_ACCOUNT_ID",
        "MUTEKI_CODEX_ACCOUNT_ID",
        "MUTEKI_CLAUDE_ACCOUNT_ID",
        "MUTEKI_CURSOR_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    run = SimpleNamespace(
        id="run-1",
        workspace_path=_workspace(),
        hints_json=(
            hints
            if hints is not None
            else write_runtime_selection({}, reason_model_config_id=None, worker_engines=tuple(selections))
        ),
        engine_type=engine_type,
        model_config_id=model_config_id,
    )
    challenge = SimpleNamespace(
        id="challenge-1",
        name="授权靶场",
        description="d",
        challenge_type="WEB_TARGET",
        target_url="http://target.test",
        allowed_hosts=["target.test"],
        flag_pattern=r"flag\{[^}]+\}",
        source_path=None,
        metadata_json={},
    )
    runtime = MutekiRuntime(None, run, challenge)
    runtime.session = _FakeSession(configs)
    return asyncio.run(runtime._worker_profiles("upstream_container"))


def test_worker_profiles_cover_every_engine_from_its_own_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    key = encrypt_api_key("sk-test")
    configs = [
        _config(id="cx", provider_type="codex_cli", base_url="https://api.openai.com/v1", encrypted_api_key=key),
        _config(id="cl", provider_type="claude_cli", base_url="https://api.anthropic.com", encrypted_api_key=key),
        # A retired channel, still selected: the run boundary must drop it rather
        # than hand a Cursor profile to the Coordinator.
        _config(id="cu", provider_type="cursor_cli"),
        _config(id="shim", provider_type="local_proxy", base_url="http://127.0.0.1:8000/v1", encrypted_api_key=key),
    ]
    profiles = _profiles(
        [
            WorkerEngineSelection("codex_cli", "cx"),
            WorkerEngineSelection("claude_cli", "cl"),
            WorkerEngineSelection("cursor_cli", "cu"),
            WorkerEngineSelection("openai_compatible", "shim"),
        ],
        configs,
        monkeypatch,
    )
    by_id = {profile.engine_id: profile for profile in profiles}
    assert list(by_id) == [
        "codex-cli:cx",
        "claude-cli:cl",
        "openai-compatible:shim",
    ]

    codex = by_id["codex-cli:cx"]
    assert codex.environment["OPENAI_API_KEY"] == "sk-test"
    assert codex.driver_profile["engine"] == "codex"
    assert codex.driver_profile["wire_api"] == "responses"
    assert codex.driver_profile["reasoning_effort"] == "medium"
    assert codex.environment["MUTEKI_WORKER_MODEL"] == "model-x"

    claude = by_id["claude-cli:cl"]
    assert claude.environment["ANTHROPIC_API_KEY"] == "sk-test"
    # The Claude CLI has no `-c` flag surface, so its endpoint travels in env.
    assert claude.environment["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert claude.driver_profile["transport"] == "claude_cli"

    # The retired channel produced no profile at all -- that is the guarantee,
    # not merely that the settings page hides it.
    assert "cursor-cli:cu" not in by_id

    shim = by_id["openai-compatible:shim"]
    # A local proxy lands on the native Chat Completions client, NOT on the Codex
    # EndpointDriver whose default wire contract is /responses.
    assert shim.driver_profile["engine"] == "openai_compatible"
    assert shim.driver_profile["protocol"] == "chat_completions"
    assert shim.driver_profile["base_url"] == "http://127.0.0.1:8000/v1"


def test_worker_profiles_use_the_account_store_for_a_local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = [
        _config(id="cl", provider_type="claude_cli"),
        _config(id="cx", provider_type="codex_cli"),
    ]
    profiles = _profiles(
        [WorkerEngineSelection("claude_cli", "cl"), WorkerEngineSelection("codex_cli", "cx")],
        configs,
        monkeypatch,
    )
    by_id = {profile.engine_id: profile for profile in profiles}
    assert by_id["claude-cli:cl"].environment["MUTEKI_CLAUDE_ACCOUNT_ID"] == "claude-main"
    assert by_id["claude-cli:cl"].environment["MUTEKI_CREDENTIAL_ACCOUNT_ID"] == "claude-main"
    assert by_id["claude-cli:cl"].driver_profile.get("base_url") is None
    assert by_id["codex-cli:cx"].environment["MUTEKI_CODEX_ACCOUNT_ID"] == "codex-main"
    assert by_id["codex-cli:cx"].driver_profile["reasoning_effort"] == "medium"


UNAVAILABLE = "MUTEKI_WORKER_SELECTION_UNAVAILABLE"


def test_a_mismatched_provider_is_refused_instead_of_rerouted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A claude_cli config can never satisfy a codex_cli selection.  The run must
    # fail closed: silently rerouting to another engine (or to the official
    # Codex fallback profile) would change the operator's roster behind their
    # back and mislabel the audit trail.
    with pytest.raises(ValueError, match=UNAVAILABLE):
        _profiles(
            [WorkerEngineSelection("codex_cli", "cl")],
            [_config(id="cl", provider_type="claude_cli")],
            monkeypatch,
        )


def test_disabled_and_missing_configs_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=UNAVAILABLE):
        _profiles(
            [WorkerEngineSelection("claude_cli", "off"), WorkerEngineSelection("claude_cli", "ghost")],
            [_config(id="off", provider_type="claude_cli", enabled=False)],
            monkeypatch,
        )


def test_a_local_proxy_config_cannot_satisfy_a_cli_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=UNAVAILABLE):
        _profiles(
            [WorkerEngineSelection("claude_cli", "shim")],
            [
                _config(
                    id="shim",
                    provider_type="local_proxy",
                    base_url="http://127.0.0.1:8000/v1",
                    encrypted_api_key=encrypt_api_key("sk-test"),
                )
            ],
            monkeypatch,
        )


def test_a_wire_worker_without_a_usable_key_is_refused_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    # decrypt_api_key raises on a falsy value, so the branch below it would be
    # dead code without the guard -- the failure would escape as
    # DomainError(API_KEY_MISSING) and abort the run instead of naming the
    # unusable selection.
    with pytest.raises(ValueError, match=UNAVAILABLE):
        _profiles(
            [WorkerEngineSelection("openai_compatible", "shim")],
            [_config(id="shim", provider_type="local_proxy", base_url="http://127.0.0.1:8000/v1")],
            monkeypatch,
        )


def test_a_partly_unusable_roster_keeps_the_usable_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = _profiles(
        [WorkerEngineSelection("claude_cli", "cl"), WorkerEngineSelection("codex_cli", "ghost")],
        [_config(id="cl", provider_type="claude_cli", encrypted_api_key=encrypt_api_key("sk-test"))],
        monkeypatch,
    )
    assert [profile.engine_id for profile in profiles] == ["claude-cli:cl"]


def test_a_run_with_no_explicit_selection_uses_the_official_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``explicit_selections`` is what separates "the operator chose nothing"
    # (a legacy run -> the official profile) from "the operator's choice was
    # unusable" (-> MUTEKI_WORKER_SELECTION_UNAVAILABLE).  Collapsing the two
    # would silently re-route a deliberately selected engine.
    profiles = _profiles([], [], monkeypatch, hints={}, model_config_id=None)
    assert [profile.engine_id for profile in profiles] == ["codex"]
    assert profiles[0].environment["MUTEKI_TARGET_URL"] == "http://target.test"

