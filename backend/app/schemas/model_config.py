from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.core.net import require_loopback_url
from app.engines.wire import CHAT_COMPLETIONS, normalize_wire_api

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max", "ultra"]
WireApi = Literal["responses", "chat_completions"]
ProviderType = Literal[
    "openai_compatible",
    "codex_cli",
    "claude_cli",
    "local_proxy",
]

# Providers that were offered in the past and are no longer selectable.
#
# ``cursor_cli`` is retired, not deleted: the headless Cursor CLI speaks a private
# ConnectRPC/protobuf API (``POST /auth/exchange_user_api_key`` then the
# ``aiserver.v1.*`` services) rather than OpenAI REST, so no ordinary relay can
# serve it and every configured endpoint failed its health probe.  Rows already in
# the database stay readable and render with their real name; what stops is
# *creating or editing into* the channel, and the settings page no longer offers
# it.  Retiring this way keeps the migration reversible: the driver, the credential
# account store and the runtime branch are all untouched, so re-enabling it later
# is a one-line change here plus a frontend entry.
RETIRED_PROVIDER_TYPES = frozenset({"cursor_cli"})

#: What an UPDATE payload may carry: everything a create may, plus the retired
#: providers, so a row that already uses one stays editable (disable it, rename
#: it, move it to another provider).  Only *entering* the channel is closed.
ProviderTypeUpdate = Literal[
    "openai_compatible",
    "codex_cli",
    "claude_cli",
    "local_proxy",
    "cursor_cli",
]

# Providers backed by a shelled CLI agent rather than an HTTP model endpoint.
# They are Worker-only: a CLI agent executes tools, so it cannot serve the
# Coordinator's read-only reasoning role.
CLI_PROVIDER_TYPES = frozenset({"codex_cli", "claude_cli", "cursor_cli"})
# Providers that speak the OpenAI Chat Completions wire protocol.  local_proxy is
# the same protocol as openai_compatible -- the distinction is entirely the
# loopback-only endpoint contract below, which is why they share this set.
OPENAI_WIRE_PROVIDER_TYPES = frozenset({"openai_compatible", "local_proxy"})
# CLI providers that accept a pinned API endpoint instead of the host's own
# subscription login.  Cursor is included: the driver carries the value in
# CURSOR_ENDPOINT, which CursorDriver emits as `--endpoint <url>`.
ENDPOINT_CAPABLE_CLI_PROVIDERS = frozenset({"codex_cli", "claude_cli", "cursor_cli"})

# Provider types that can back a Worker. Every ProviderType qualifies today --
# what matters when filtering is the *engine* it produces
# (RUNTIME_ENGINE_TYPE_BY_PROVIDER), but a query on the ``provider_type`` column
# needs the provider-side equivalent.
WORKER_PROVIDER_TYPES = OPENAI_WIRE_PROVIDER_TYPES | CLI_PROVIDER_TYPES

# provider_type -> the Worker engine type it produces at run time.
#
# local_proxy deliberately maps onto openai_compatible: it IS the OpenAI wire
# protocol, and the only thing that makes it a separate provider is the
# loopback-only endpoint contract enforced below.  Keeping it out of the runtime
# engine vocabulary means the Coordinator, hints_json selection and WorkerPool
# need no fifth engine identity for what is the same execution path.
RUNTIME_ENGINE_TYPE_BY_PROVIDER: dict[str, str] = {
    "openai_compatible": "openai_compatible",
    "local_proxy": "openai_compatible",
    "codex_cli": "codex_cli",
    "claude_cli": "claude_cli",
    "cursor_cli": "cursor_cli",
}


def runtime_engine_type(provider_type: str) -> str:
    """Map a stored ``provider_type`` onto its Worker engine type."""

    return RUNTIME_ENGINE_TYPE_BY_PROVIDER.get(str(provider_type or ""), "openai_compatible")


# ── Worker engine vocabulary ─────────────────────────────────────────────────
# The run-time vocabulary the scheduler binds a model config to. Deliberately
# NOT the same set as ProviderType: ``local_proxy`` is a *provider*, not an
# engine -- it executes through the openai_compatible engine (see
# RUNTIME_ENGINE_TYPE_BY_PROVIDER), so it must never appear here.
WORKER_ENGINE_TYPES = frozenset(
    {"openai_compatible", "codex_cli", "claude_cli", "cursor_cli"}
)
# Engine types backed by a shelled CLI agent. Worker-only, and the ones that own
# a local sandbox -- so they are the ones the "Muteki Workers only" guards apply
# to rather than a single hard-coded engine.
CLI_WORKER_ENGINE_TYPES = frozenset({"codex_cli", "claude_cli", "cursor_cli"})
# Every spelling the Run API and the frontend have used for an engine over time.
WORKER_ENGINE_ALIASES = {
    "codex": "codex_cli",
    "codex-sdk": "codex_cli",
    "codex_sdk": "codex_cli",
    "codex-cli": "codex_cli",
    "codex-api": "codex_cli",
    "claude": "claude_cli",
    "claude-cli": "claude_cli",
    "claude-code": "claude_cli",
    "claude_code": "claude_cli",
    "cursor": "cursor_cli",
    "cursor-cli": "cursor_cli",
    "cursor-agent": "cursor_cli",
    "cursor_agent": "cursor_cli",
    "openai-compatible": "openai_compatible",
    "openai_compatible": "openai_compatible",
    # A provider name leaking in as an engine name still resolves to the engine
    # it actually executes through, so the mapping has exactly one definition.
    "local-proxy": "openai_compatible",
    "local_proxy": "openai_compatible",
}
# engine_type -> the identity prefix Coordinator scheduling uses. Distinct per
# engine, so two configs on different engines never collapse into one Worker.
WORKER_ENGINE_ID_PREFIX = {
    "openai_compatible": "openai-compatible",
    "codex_cli": "codex-cli",
    "claude_cli": "claude-cli",
    "cursor_cli": "cursor-cli",
}


def normalize_worker_engine_type(value: object) -> str:
    """Case/alias-normalize an engine name. Returns ``""`` when unrecognized."""

    normalized = str(value or "").strip().casefold()
    resolved = WORKER_ENGINE_ALIASES.get(normalized, normalized)
    return resolved if resolved in WORKER_ENGINE_TYPES else ""


class ModelConfigWrite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: Whether an endpoint-pinned CLI provider must carry an api_key in THIS
    #: payload.  A create must (there is no stored secret to fall back on); an
    #: update must not, because omitting it means "keep the stored one" and the
    #: handler implements exactly that.  Without the distinction the settings
    #: page could not save any edit to a claude/cursor/codex config that has a
    #: base_url -- it 422'd with "requires api_key" even though the field is
    #: labelled "leave blank to keep".
    _require_api_key: ClassVar[bool] = True

    #: Whether a retired provider may appear in THIS payload.  A create must not
    #: (the channel is withdrawn); an update must be allowed to, or the rows that
    #: already exist could never be disabled or renamed again -- they would be
    #: frozen in whatever state they were left.  The run-time roster is what
    #: actually guarantees a retired channel never executes (see
    #: ``_worker_profiles``), so allowing the edit does not reopen the channel.
    _reject_retired_provider: ClassVar[bool] = True

    name: str = Field(min_length=1, max_length=200)
    provider_type: ProviderType = "openai_compatible"

    @field_validator("provider_type", mode="before")
    @classmethod
    def reject_retired_provider(cls, value: object) -> object:
        """Say WHY a retired channel is refused, instead of an enum mismatch.

        A bare ``Literal`` rejection reads as "input should be one of ...", which
        tells an operator nothing about the channel having been withdrawn.
        """

        text = str(value or "").strip()
        if cls._reject_retired_provider and text in RETIRED_PROVIDER_TYPES:
            raise ValueError(
                f"{text} is no longer offered: the Cursor CLI speaks a private "
                "ConnectRPC API (POST /auth/exchange_user_api_key, then the "
                "aiserver.v1.* services) that an OpenAI-compatible relay cannot "
                "serve. Existing configurations remain readable; pick another "
                "provider for new ones."
            )
        return value
    base_url: HttpUrl | None = None
    wire_api: WireApi | None = None
    model_name: str = Field(min_length=1, max_length=255, pattern=r"^\S+$")
    reasoning_effort: ReasoningEffort | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool = True
    roles: list[Literal["worker", "coordinator_reason"]] = Field(default_factory=lambda: ["worker"], max_length=2)
    action_protocol: str = Field(default="json_schema", pattern="^(json_schema|json_object|prompt_json|native_tool_call)$")
    structured_output_mode: str = Field(default="json_schema", pattern="^(json_schema|json_object|prompt_json)$")
    request_timeout_seconds: int = Field(default=30, ge=5, le=600)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=1.0, ge=0.1, le=60)
    rate_limit_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    requests_per_minute: int = Field(default=60, ge=1, le=10000)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    context_token_limit: int = Field(default=128000, ge=1024, le=1000000)

    #: Route this CLI's traffic through the local protocol-translating proxy.
    #:
    #: A first-class setting, not a hand-patched ``capabilities_json`` key.  When
    #: ``true`` the Worker's traffic is routed through the local
    #: protocol-translating proxy for its CLI, which speaks the CLI's own wire
    #: protocol and translates to the OpenAI chat wire upstream.  Which proxy is
    #: meant depends on the provider: ``claude_cli`` uses the Anthropic Messages
    #: one, ``codex_cli`` uses the Responses one.  Required for any relay that
    #: rejects the CLI's native protocol -- most groups do (Claude: "This group
    #: does not allow /v1/messages dispatch"; Codex: "本站不提供
    #: Responses(reasoning) 协议"), where a direct CLI connection can never work.
    #:
    #: ``None`` means "leave whatever is stored" -- so an unrelated edit (or a
    #: capability probe, which seeds from stored capabilities and rewrites only
    #: what it measures) cannot silently drop the flag and reroute a working
    #: config into a direct connection that the upstream refuses.
    route_via_proxy: bool | None = None

    #: ``claude official name -> upstream model name`` for the proxy to resolve.
    #:
    #: The Claude CLI only ever sends names from its own built-in list
    #: (``claude-sonnet-5`` and friends), which no third-party relay recognises.
    #: This map is how the proxy both translates the name and picks which stored
    #: configuration serves the request.  ``None`` keeps the stored value.
    model_aliases: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_provider_contract(self) -> "ModelConfigWrite":
        if self.provider_type in OPENAI_WIRE_PROVIDER_TYPES and self.base_url is None:
            raise ValueError(f"{self.provider_type} model requires base_url")
        if self.provider_type == "local_proxy":
            # A local reverse proxy has no authentication of its own, so a
            # non-loopback host would expose it to the network.  See app.core.net
            # for why the check is positive (loopback) rather than a blocklist.
            require_loopback_url(str(self.base_url))
        if self.provider_type in CLI_PROVIDER_TYPES and any(role != "worker" for role in self.roles):
            raise ValueError(f"{self.provider_type} models are Worker-only")
        if self.provider_type == "codex_cli" and self.reasoning_effort is None:
            self.reasoning_effort = "medium"
        if (
            self._require_api_key
            and self.provider_type in ENDPOINT_CAPABLE_CLI_PROVIDERS
            and self.base_url is not None
            and self.api_key is None
        ):
            raise ValueError(f"{self.provider_type} API endpoint requires api_key")
        if self.provider_type == "codex_cli":
            if self.base_url is not None:
                # Codex CLI only accepts `wire_api = "responses"` in its provider
                # config.  `"chat"` was removed upstream ("`wire_api = "chat"` is
                # no longer supported") and `"chat_completions"` was never a valid
                # variant at all -- codex aborts at config load with
                # ``unknown variant `chat_completions`, expected `responses` ``,
                # so the Worker never even reaches the network.  The literal
                # below therefore keeps accepting the legacy spelling (so old
                # forms do not 422) but every codex endpoint is pinned here.
                self.wire_api = "responses"
        elif self.provider_type in OPENAI_WIRE_PROVIDER_TYPES:
            # An OpenAI-wire provider may serve either dialect.  Some relays only
            # expose `/chat/completions`, others only `/responses`; defaulting to
            # Chat Completions keeps every existing configuration working, and an
            # unrecognised value falls back rather than 422-ing a legacy row.
            self.wire_api = normalize_wire_api(self.wire_api, default=CHAT_COMPLETIONS)
        else:
            # A CLI provider owns its own transport, so a wire dialect is
            # meaningless and must not be persisted.
            self.wire_api = None
        return self


class ModelConfigUpdate(ModelConfigWrite):
    #: An omitted api_key on an update means "keep the stored secret", so the
    #: create-time requirement must not apply (see the flag's definition).
    _require_api_key: ClassVar[bool] = False
    #: See the flag's definition: an existing retired row must stay editable so it
    #: can be disabled or renamed.
    _reject_retired_provider: ClassVar[bool] = False

    # Widened vs the create contract so an existing retired row stays editable;
    # see ProviderTypeUpdate.
    provider_type: ProviderTypeUpdate = "openai_compatible"
    api_key: str | None = Field(default=None, max_length=1000)
