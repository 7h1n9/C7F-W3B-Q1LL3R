"""The Cursor channel is retired.

The headless Cursor CLI speaks a private ConnectRPC/protobuf API -- first
``POST /auth/exchange_user_api_key``, then the ``aiserver.v1.*`` services -- so no
OpenAI-compatible relay can serve it and every configured endpoint failed its
health probe.  Retirement has to satisfy two constraints that pull in opposite
directions:

* the channel must not be *entered* (no new configuration, not selectable as a
  Worker engine), and
* the rows already in the database must stay *manageable* (disable, rename,
  move to another provider) -- blocking updates too would freeze them forever in
  whatever state they were left.

The guarantee that it never executes lives at the run boundary instead, so
neither constraint has to be traded away.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.schemas.model_config import (
    RETIRED_PROVIDER_TYPES,
    ModelConfigUpdate,
    ModelConfigWrite,
    ProviderType,
    ProviderTypeUpdate,
)
from app.schemas.run import WorkerEngineSelection


def test_cursor_is_declared_retired_and_gone_from_the_create_contract() -> None:
    assert "cursor_cli" in RETIRED_PROVIDER_TYPES
    assert "cursor_cli" not in ProviderType.__args__
    # ...but still accepted on an update, so existing rows stay editable.
    assert "cursor_cli" in ProviderTypeUpdate.__args__


def test_creating_a_retired_channel_is_refused_with_a_reason() -> None:
    with pytest.raises(ValidationError, match="no longer offered"):
        ModelConfigWrite(
            name="Cursor",
            provider_type="cursor_cli",  # type: ignore[arg-type]
            base_url="https://relay.example/v1",
            model_name="m",
            api_key="sk-test",
            roles=["worker"],
        )


def test_an_existing_retired_row_can_still_be_disabled() -> None:
    """Otherwise the operator could never switch the channel off."""

    updated = ModelConfigUpdate(
        name="ap-cursor",
        provider_type="cursor_cli",  # type: ignore[arg-type]
        base_url="https://relay.example/v1",
        model_name="m",
        roles=["worker"],
        enabled=False,
    )
    assert updated.enabled is False


def test_a_retired_channel_is_not_selectable_as_a_worker_engine() -> None:
    with pytest.raises(ValidationError):
        WorkerEngineSelection(engine_type="cursor_cli", model_config_id="cfg-1")  # type: ignore[arg-type]


def test_the_offered_providers_are_unaffected() -> None:
    """Retiring one channel must not narrow the others."""

    for provider in ("openai_compatible", "local_proxy"):
        assert ModelConfigWrite(
            name=f"{provider}",
            provider_type=provider,  # type: ignore[arg-type]
            base_url="http://127.0.0.1:8787/v1",
            model_name="m",
            api_key="sk-test",
            roles=["worker"],
        )
    assert ModelConfigWrite(
        name="claude",
        provider_type="claude_cli",
        base_url="https://api.anthropic.com",
        model_name="claude-sonnet-4-5",
        api_key="sk-test",
        roles=["worker"],
    )


def test_the_runtime_roster_refuses_a_retired_provider() -> None:
    """The run boundary is where "never executes" is actually guaranteed.

    A hand-edited ``hints_json``, a replayed old Run, or a config switched back to
    the retired provider all reach ``_worker_profiles``; the schema cannot see any
    of them.  Asserting on the source keeps this honest without standing up a
    database, and fails loudly if the guard is dropped during a refactor.
    """

    from app.solver.muteki.runtime import muteki_runtime

    source = inspect.getsource(muteki_runtime.MutekiRuntime._worker_profiles)
    assert "RETIRED_PROVIDER_TYPES" in source
    assert "config.provider_type" in source
