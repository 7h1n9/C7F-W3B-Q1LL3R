"""The legacy Kali Runner is retired, and nothing may gate on it any more.

Why this needs pinning
----------------------
Muteki executes tools in container-native Workers.  The standalone Kali Runner
was still wired into three *gating* paths, and each of them failed in the same
shape: an absent service silently produced an empty capability set, which the
code then read as "the deployment is broken".

* **Tool manifest.**  ``effective = role & challenge_tools & backend & runner``.
  With no Runner, ``runner`` was empty, so every Run started with no tools and was
  paused with ``TOOL_CATALOG_DRIFT`` -- a message about the tool catalog whose
  cause was a service the deployment does not use.
* **Tool permissions.**  Same intersection on the per-request path.
* **Readiness.**  Five ``runner*`` checks turned the console red with a 15s
  outbound timeout per page load, reading as "the system is down" rather than
  "an unused optional service is absent".

Plus a fourth, unrelated rot this file also pins: ``EXPECTED_REVISION`` was a
literal that fell 24 migrations behind, so a fully-migrated database was reported
as ``NOT_READY``.  A check whose own value is the stale thing is worse than none.

The flag is the contract: ``APP_LEGACY_KALI_RUNNER=1`` restores the old behaviour
exactly, so a deployment that *does* front a Runner must keep working.  Both
directions are asserted below.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import readiness
from app.core.config import get_settings
from app.services import tool_manifest, tool_permissions


def _flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """Pin the deployment flag without touching the real environment."""
    settings = get_settings()
    monkeypatch.setattr(settings, "legacy_kali_runner", enabled, raising=False)


class _FakeSession:
    """The slice of ``AsyncSession`` the manifest path touches.

    ``commit``/``refresh`` are here because the drift branch emits an event, which
    persists then re-reads -- omitting them would make the enabled-Runner tests
    fail on the fake rather than on the behaviour they assert.  ``refresh`` also
    fills in the two server-defaulted columns the event serializer reads
    (``id``, ``created_at``), since a real database would.
    """

    def __init__(self) -> None:
        self.scalar = AsyncMock(return_value=None)
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.refresh = AsyncMock(side_effect=self._populate_defaults)
        self.item = None

    async def _populate_defaults(self, instance) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = "event-1"
        if getattr(instance, "created_at", None) is None:
            instance.created_at = datetime.now(UTC)

    def add(self, item) -> None:
        self.item = item


def _manifest_inputs(tools: list[str], *, adapter: str | None = None):
    run = SimpleNamespace(
        id="run-1",
        role_snapshot_json={"tools": list(tools)},
        engine_type="codex_cli",
        solver_mode="muteki",
        # The manifest writes status/error fields onto the Run when it decides a
        # Run must be paused, so those attributes have to exist here too.
        status="RUNNING",
        last_error_code=None,
        last_error_message=None,
    )
    attempt = SimpleNamespace(id="attempt-1", runtime_build_manifest_json={})
    metadata = {"adapter": adapter} if adapter else {}
    challenge = SimpleNamespace(id="challenge-1", challenge_type="WEB_TARGET", metadata_json=metadata)
    return run, attempt, challenge


# --------------------------------------------------------------------------- #
# the manifest must not empty itself over an absent Runner
# --------------------------------------------------------------------------- #


async def test_manifest_keeps_tools_without_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: no Runner used to mean no tools at all.

    ``runner_client`` is left unpatched on purpose -- in this deployment it is
    pointed at an absent host, which is exactly the condition that used to empty
    the effective set.  The backend registry is now authoritative, so the tools
    survive it.
    """
    _flag(monkeypatch, False)
    session = _FakeSession()
    run, attempt, challenge = _manifest_inputs(["http_request", "sql_boolean_compare"])

    manifest = await tool_manifest.refresh_runtime_tool_manifest(session, run, attempt, challenge)

    assert manifest.effective_tools, "a retired Runner emptied the effective tool set"
    assert "http_request" in manifest.effective_tools
    assert "sql_boolean_compare" in manifest.effective_tools
    assert manifest.missing_expected_tools == [], "network tools were reported missing without a Runner"
    assert attempt.tool_manifest_status == "READY"


async def test_manifest_does_not_pause_the_run_without_a_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PAUSED_DEPLOYMENT`` here is the failure an operator could never explain.

    The Run is paused with ``TOOL_CATALOG_DRIFT``, so every diagnostic points at
    the tool catalog and none of them at the real cause.
    """
    _flag(monkeypatch, False)
    session = _FakeSession()
    run, attempt, challenge = _manifest_inputs(["http_request", "script_run"])

    await tool_manifest.refresh_runtime_tool_manifest(session, run, attempt, challenge)

    assert run.status != "PAUSED_DEPLOYMENT", "a retired Runner paused a healthy Run"


async def test_manifest_still_gates_on_a_runner_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opting back in must restore the old behaviour exactly.

    The intersection *is* the manifest's purpose when a Runner is in the path, so
    a Runner that advertises fewer tools than the registry must still narrow the
    effective set -- otherwise this fix would have silently disabled the check.
    """
    _flag(monkeypatch, True)
    payload = {
        "tools": [
            {"name": "http_request", "implemented": True, "installed": True, "enabled": True, "self_test_ok": True}
        ],
        "network_enforcement": {},
    }
    monkeypatch.setattr(tool_manifest.runner_client, "health", AsyncMock(return_value={"build": {}}))
    monkeypatch.setattr(tool_manifest.runner_client, "capabilities", AsyncMock(return_value=payload))
    session = _FakeSession()
    run, attempt, challenge = _manifest_inputs(["http_request", "sql_boolean_compare"])

    manifest = await tool_manifest.refresh_runtime_tool_manifest(session, run, attempt, challenge)

    assert "http_request" in manifest.effective_tools
    assert "sql_boolean_compare" not in manifest.effective_tools, (
        "the Runner intersection stopped narrowing the tool set"
    )
    assert "sql_boolean_compare" in manifest.missing_expected_tools


async def test_manifest_restores_allowlist_gating_when_a_runner_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Runner that cannot prove allowlist enforcement must still block the Run."""
    _flag(monkeypatch, True)
    payload = {
        "tools": [
            {
                "name": "script_run",
                "implemented": True,
                "installed": True,
                "enabled": True,
                "self_test_ok": True,
                "supported_network_modes": [],
                "target_allowlist_enforced": False,
            }
        ],
        "network_enforcement": {"target_allowlist_enforced": False},
    }
    monkeypatch.setattr(tool_manifest.runner_client, "health", AsyncMock(return_value={"build": {}}))
    monkeypatch.setattr(tool_manifest.runner_client, "capabilities", AsyncMock(return_value=payload))
    session = _FakeSession()
    run, attempt, challenge = _manifest_inputs(["script_run"])

    await tool_manifest.refresh_runtime_tool_manifest(session, run, attempt, challenge)

    assert run.status == "PAUSED_DEPLOYMENT"
    assert run.last_error_code == "SCRIPT_TARGET_NETWORK_UNAVAILABLE"


async def test_manifest_does_not_require_allowlist_proof_without_a_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend's own Gateway is the enforcement point once the Runner is gone.

    Treating "no Runner self-report" as "unproven" would pause every networked Run
    for a service that is not in the execution path.
    """
    _flag(monkeypatch, False)
    session = _FakeSession()
    run, attempt, challenge = _manifest_inputs(["script_run"])

    manifest = await tool_manifest.refresh_runtime_tool_manifest(session, run, attempt, challenge)

    assert run.status != "PAUSED_DEPLOYMENT"
    assert manifest.tool_capabilities_json["script_run"]["valid"] is True


# --------------------------------------------------------------------------- #
# the per-request permission path
# --------------------------------------------------------------------------- #


async def test_permissions_keep_tools_without_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same intersection, same silent emptying, on the per-request path.

    Which is why it is tested separately: the manifest gates the start of a Run
    and this gates every call inside it, so fixing one does not fix the other.
    """
    _flag(monkeypatch, False)
    session = _FakeSession()
    run = SimpleNamespace(
        id="run-1",
        role_snapshot_json={"tools": ["http_request", "sql_boolean_compare"]},
        solver_mode="muteki",
    )
    challenge = SimpleNamespace(challenge_type="WEB_TARGET", metadata_json={})

    allowed = await tool_permissions.effective_tools_for(session, run, challenge)

    assert allowed, "a retired Runner emptied the allowed tool set"
    assert "http_request" in allowed
    assert "sql_boolean_compare" in allowed


# --------------------------------------------------------------------------- #
# readiness: the page an operator trusts to say "the system is fine"
# --------------------------------------------------------------------------- #


def test_readiness_reports_the_tool_channel_not_a_runner() -> None:
    _flag_disabled = readiness._tool_channel_checks()
    names = {check["name"] for check in _flag_disabled}

    assert "tool_channel" in names
    assert "docker_cli" in names
    assert not (names & {"runner", "runner_token", "web_tool", "tshark", "capinfos"})


def test_readiness_gate_checks_exclude_the_runner() -> None:
    """A check that is not emitted must not be able to block readiness.

    Leaving ``runner`` in the gate set would have been a silent trap: the check
    disappears from the payload and ``all(...)`` over an empty selection is
    ``True``, so it would have looked fine -- until someone re-added the check.
    """
    assert "runner" not in readiness.WEB_GATE_CHECKS
    assert "runner_token" not in readiness.WEB_GATE_CHECKS
    assert "web_tool" not in readiness.WEB_GATE_CHECKS
    assert "runner" not in readiness.TRAFFIC_GATE_CHECKS


def test_expected_revision_follows_the_migration_scripts() -> None:
    """The constant that rotted 24 migrations behind.

    Asserted against the actual head the scripts report, so adding a migration
    cannot make this check lie again.
    """
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_root = Path(readiness.__file__).resolve().parents[3]
    head = ScriptDirectory.from_config(Config(str(backend_root / "alembic.ini"))).get_current_head()

    assert readiness.expected_revision() == head
    assert head, "no alembic head found"


# --------------------------------------------------------------------------- #
# the flag itself
# --------------------------------------------------------------------------- #


def test_legacy_runner_is_off_by_default() -> None:
    """Default matters: it is what every existing deployment gets on upgrade."""
    from app.core.config import Settings

    assert "legacy_kali_runner" in Settings.model_fields
    assert Settings.model_fields["legacy_kali_runner"].default is False