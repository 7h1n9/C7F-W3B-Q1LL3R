"""Routing must be a first-class, operator-controlled setting.

The failure this file pins down
-------------------------------
``route_via_proxy`` and ``model_aliases`` decide whether a ``claude_cli`` Worker
talks to the upstream relay directly or through the local protocol-translating
proxy.  They lived only as hand-written ``capabilities_json`` keys with no API
surface:

* ``ModelConfigWrite`` had neither field, and ``extra="ignore"`` silently
  dropped them from any payload that carried them;
* ``read()`` did not return them, so the settings page could not show their
  state;
* the only way to set them was a one-off SQL patch (``scripts/_fix_claude_deep_caps.sql``),
  which is why one config worked and a second, identically-shaped one did not.

A relay that refuses ``/v1/messages`` (many groups do: "This group does not
allow /v1/messages dispatch") can *only* be reached through the proxy, so a
config that cannot express ``route_via_proxy`` is a config that cannot be made
to work at all.  These tests pin the write path, the read path, and the two
"do not silently clear it" guarantees.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.v1 import models
from app.schemas.model_config import ModelConfigUpdate, ModelConfigWrite

_ALIASES = {
    "claude-sonnet-5": "glm-5.3",
    "claude-haiku-4-5": "glm-5.3",
}


def _payload(**overrides: object) -> ModelConfigWrite:
    base: dict[str, object] = {
        "name": "AP",
        "provider_type": "claude_cli",
        "base_url": "https://sub.gavinchen.top/v1/chat/completions",
        "model_name": "glm-5.3",
        "api_key": "sk-test",
    }
    base.update(overrides)
    return ModelConfigWrite(**base)


# --------------------------------------------------------------------------- #
# the schema accepts the fields instead of dropping them
# --------------------------------------------------------------------------- #


def test_write_schema_keeps_the_routing_fields() -> None:
    """``extra="ignore"`` used to drop them; they are now declared."""
    config = _payload(route_via_proxy=True, model_aliases=_ALIASES)
    assert config.route_via_proxy is True
    assert config.model_aliases == _ALIASES


def test_routing_fields_default_to_none_meaning_leave_as_is() -> None:
    """Omitted must mean "unchanged", never "off"."""
    config = _payload()
    assert config.route_via_proxy is None
    assert config.model_aliases is None


def test_update_schema_also_carries_the_fields() -> None:
    config = ModelConfigUpdate(
        name="AP",
        provider_type="claude_cli",
        base_url="https://sub.gavinchen.top/v1/chat/completions",
        model_name="glm-5.3",
        route_via_proxy=True,
        model_aliases=_ALIASES,
    )
    assert config.route_via_proxy is True
    assert config.model_aliases == _ALIASES


# --------------------------------------------------------------------------- #
# the fold into capabilities_json
# --------------------------------------------------------------------------- #


def test_routing_settings_are_written_into_capabilities() -> None:
    capabilities = models._apply_routing_capabilities(
        {"roles": ["worker"]}, _payload(route_via_proxy=True, model_aliases=_ALIASES)
    )
    assert capabilities["route_via_proxy"] is True
    assert capabilities["model_aliases"] == _ALIASES


def test_omitting_the_settings_preserves_what_is_stored() -> None:
    """A capability probe re-sends a payload without these keys; a partial edit
    does too.  Either must leave a working route alone rather than rerouting the
    config into a direct connection the upstream refuses."""
    stored = {"route_via_proxy": True, "model_aliases": dict(_ALIASES)}
    capabilities = models._apply_routing_capabilities(dict(stored), _payload())
    assert capabilities["route_via_proxy"] is True
    assert capabilities["model_aliases"] == _ALIASES


def test_explicit_false_turns_the_proxy_off() -> None:
    stored = {"route_via_proxy": True, "model_aliases": dict(_ALIASES)}
    capabilities = models._apply_routing_capabilities(dict(stored), _payload(route_via_proxy=False))
    assert capabilities["route_via_proxy"] is False


def test_empty_alias_map_clears_the_aliases() -> None:
    """An empty map is an instruction ("drop them"), not "leave them"."""
    stored = {"model_aliases": dict(_ALIASES)}
    capabilities = models._apply_routing_capabilities(dict(stored), _payload(model_aliases={}))
    assert "model_aliases" not in capabilities


def test_blank_alias_entries_are_dropped() -> None:
    capabilities = models._apply_routing_capabilities(
        {}, _payload(model_aliases={" claude-sonnet-5 ": " glm-5.3 ", "": "x", "claude-opus-5": " "})
    )
    assert capabilities["model_aliases"] == {"claude-sonnet-5": "glm-5.3"}


# --------------------------------------------------------------------------- #
# the read path surfaces them
# --------------------------------------------------------------------------- #


def _read_row(**capabilities: object) -> dict:
    item = SimpleNamespace(
        id="cfg-1",
        name="AP",
        provider_type="claude_cli",
        base_url="https://sub.gavinchen.top/v1/chat/completions",
        wire_api=None,
        model_name="glm-5.3",
        enabled=True,
        encrypted_api_key="enc",
        action_protocol="json_schema",
        structured_output_mode="json_schema",
        supports_json_schema=False,
        supports_json_object=False,
        supports_native_tool_call=True,
        request_timeout_seconds=30,
        max_output_tokens=2048,
        temperature=0.0,
        max_retries=2,
        retry_base_seconds=1.0,
        rate_limit_cooldown_seconds=60,
        requests_per_minute=60,
        max_concurrency=2,
        context_token_limit=128000,
        last_test_at=None,
        last_test_ok=None,
        capabilities_json=dict(capabilities),
    )
    return models.read(item)


def test_read_returns_the_routing_settings() -> None:
    row = _read_row(route_via_proxy=True, model_aliases=dict(_ALIASES))
    assert row["route_via_proxy"] is True
    assert row["model_aliases"] == _ALIASES


def test_read_defaults_a_config_without_them() -> None:
    row = _read_row()
    assert row["route_via_proxy"] is False
    assert row["model_aliases"] == {}


def test_read_tolerates_a_malformed_alias_value() -> None:
    """A hand-written JSON blob must not crash the settings page."""
    row = _read_row(model_aliases="not-a-mapping")
    assert row["model_aliases"] == {}
