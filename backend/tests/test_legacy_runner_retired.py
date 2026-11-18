"""The legacy Kali Runner is retired, and nothing may keep depending on it.

Muteki executes tools in container-native Workers, but the Runner remained wired
into three places that all failed in the same quiet way: **an absent optional
service looked like a broken deployment.**

* ``readiness`` emitted five ``runner*`` checks and failed them when the Runner
  was unreachable, so the console showed ``NOT_READY`` with five red cards.
* ``tool_manifest`` intersected the effective tool set with the Runner's
  advertised capabilities -- so an unreachable Runner emptied every Run's tool
  list and paused it with ``TOOL_CATALOG_DRIFT``, a message that points at the
  tool catalog while the cause is a service nobody is using.
* ``tool_permissions`` did the same intersection on the ad-hoc path.
* ``system_settings`` spent a 15-second probe on every page load asking the
  absent service whether it was absent.

``APP_LEGACY_KALI_RUNNER`` is the single switch.  These tests pin both positions:
off (the default, and the only behaviour a new deployment sees) and on (the old
behaviour, unchanged, for a deployment that still fronts a Runner).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import readiness as readiness_module
from app.core.config import get_settings
from app.services import tool_manifest, tool_permissions

# --------------------------------------------------------------------------- #
# readiness: the Runner must not gate a solve
# --------------------------------------------------------------------------- #


async def test_readiness_reports_the_tool_channel_not_a_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, no check is named after the retired service."""

    monkeypatch.setattr(readiness_module, "get_settings", lambda: _settings(False))
    # The Runner probe must not even be attempted -- its presence in the output
    # would put a red card in front of the operator for a service out of scope.
    monkeypatch.setattr(
        readiness_module, "_runner_checks", AsyncMock(side_effect=AssertionError("probed"))
    )

    class FakeSession:
        async def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: readiness_module.expected_revision())

        async def scalars(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: [SimpleNamespace(id="x")])

    payload = (await readiness_module.range_test_readiness(FakeSession()))["data"]
    names = {check["name"] for check in payload["checks"]}

    assert names.isdisjoint({"runner", "runner_token", "web_tool", "tshark", "capinfos"})
    assert "tool_channel" in names
    assert payload["ready"] is True, "a healthy deployment must not be blocked by a retired service"


async def test_readiness_still_gates_on_a_runner_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old behaviour is restored exactly for a deployment that front one."""
    monkeypatch.setattr(readiness_module, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(
        readiness_module,
        "_runner_checks",
        AsyncMock(
            return_value=[
                {"name": "runner", "ok": False, "message": "Runner unavailable: refused"}
            ]
        ),
    )

    class FakeSession:
        async def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: readiness_module.expected_revision())

        async def scalars(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: [SimpleNamespace(id="x")])

    payload = (await readiness_module.range_test_readiness(FakeSession()))["data"]
    names = {check["name"] for check in payload["checks"]}

    assert "runner" in names
    assert "tool_channel" not in names


def test_expected_revision_follows_the_migration_scripts() -> None:
    """The check must not rot.

    It was pinned to a literal and ended up 24 migrations behind, so a correctly
    migrated database was reported as ``NOT_READY``.  A readiness check that is
    the thing out of date teaches the operator to ignore the page.

    The expected value is derived here from the migration files themselves --
    the head is the revision no other revision lists as its ``down_revision`` --
    so this test cannot itself go stale the way the constant did.
    """
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    revisions: set[str] = set()
    referenced: set[str] = set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if revision:
            revisions.add(revision.group(1))
        if down:
            referenced.add(down.group(1))

    heads = revisions - referenced
    assert len(heads) == 1, f"expected exactly one head, found {sorted(heads)}"
    assert readiness_module.expected_revision() == heads.pop()


def test_expected_revision_is_unknown_rather_than_wrong_when_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken probe reads as "unknown", which the caller must not treat as drift."""
    import alembic.script

    monkeypatch.setattr(
        alembic.script.ScriptDirectory,
        "from_config",
        classmethod(lambda _cls, _config: (_ for _ in ()).throw(RuntimeError("no scripts"))),
    )

    assert readiness_module.expected_revision() is None


# --------------------------------------------------------------------------- #
# tool manifest: the intersection must not be applied to a service that is absent
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __init__(self) -> None:
        self.item = None
        self.commits = 0

    async def scalar(self, _statement: object) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _item: object) -> None:
        return None

    def add(self, item: object) -> None:
        self.item = item


def _manifest_fixture() -> tuple[object, object, object]:
    run = SimpleNamespace(
        id="run-1",
        role_snapshot_json={"tools": ["http_request", "sql_boolean_compare"]},
        engine_type="codex_cli",
    )
    attempt = SimpleNamespace(id="attempt-1", runtime_build_manifest_json={})
    challenge = SimpleNamespace(id="c-1", challenge_type="WEB_TARGET", metadata_json={})
    return run, attempt, challenge


async def test_manifest_does_not_pause_a_run_when_the_runner_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the failure that made every tool-driven Run unusable.

    The Runner intersection ran unconditionally.  With no Runner the capability
    set was empty, ``effective`` became empty, everything expected was reported
    missing, and the Run was paused with ``TOOL_CATALOG_DRIFT``.
    """
    monkeypatch.setattr(
        tool_manifest, "get_settings", lambda: _settings(False)
    )
    # An unreachable Runner is the realistic case -- and it must not be consulted.
    monkeypatch.setattr(
        tool_manifest.runner_client,
        "capabilities",
        AsyncMock(side_effect=AssertionError("the retired Runner was probed")),
    )
    monkeypatch.setattr(
        tool_manifest.runner_client,
        "health",
        AsyncMock(side_effect=AssertionError("the retired Runner was probed")),
    )
    run, attempt, challenge = _manifest_fixture()

    manifest = await tool_manifest.refresh_runtime_tool_manifest(
        _FakeSession(), run, attempt, challenge
    )

    assert manifest.missing_expected_tools == []
    assert "http_request" in manifest.effective_tools
    assert manifest.tool_capabilities_json["script_run"]["valid"] is True


async def test_manifest_still_intersects_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Runner gate is the manifest's purpose; opting in keeps it intact."""
    monkeypatch.setattr(tool_manifest, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(tool_manifest.runner_client, "health", AsyncMock(return_value={"build": {}}))
    monkeypatch.setattr(
        tool_manifest.runner_client,
        "capabilities",
        AsyncMock(return_value={"tools": [
            {"name": "http_request", "implemented": True, "installed": True, "enabled": True, "self_test_ok": True},
        ]}),
    )
    run, attempt, challenge = _manifest_fixture()
    # The drift event is persisted through the real service, which needs a real
    # session for column defaults.  The subject here is the intersection, so the
    # event sink is stubbed rather than emulated.
    monkeypatch.setattr(tool_manifest.event_service, "append", AsyncMock())

    manifest = await tool_manifest.refresh_runtime_tool_manifest(
        _FakeSession(), run, attempt, challenge
    )

    assert set(manifest.runner_capability_tools) == {"http_request"}
    # sql_boolean_compare is expected but the Runner does not advertise it, so it
    # is reported missing -- exactly the pre-existing contract.
    assert "sql_boolean_compare" in manifest.missing_expected_tools
    assert "http_request" in manifest.effective_tools


async def test_permissions_do_not_consult_the_runner_when_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ad-hoc path had the same silent intersection."""
    monkeypatch.setattr(tool_permissions, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(
        tool_permissions,
        "allowed_tools_for",
        lambda *_args, **_kwargs: {"http_request", "sql_boolean_compare"},
    )

    class FakeSession:
        async def scalar(self, _statement: object) -> None:
            return None

        async def scalars(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: [])

    run = SimpleNamespace(id="run-1", role_snapshot_json={"tools": ["http_request"]}, solver_mode="muteki")
    challenge = SimpleNamespace(challenge_type="WEB_TARGET", metadata_json={})

    allowed = await tool_permissions.effective_tools_for(FakeSession(), run, challenge)

    # Without the intersection, the role's tools survive.
    assert "http_request" in allowed


# --------------------------------------------------------------------------- #
# settings: no 15-second probe of a service that is out of scope
# --------------------------------------------------------------------------- #


async def test_settings_does_not_probe_the_retired_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1 import system_settings

    monkeypatch.setattr(system_settings, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(
        system_settings, "probe", AsyncMock(side_effect=AssertionError("probed an absent service"))
    )

    class FakeSession:
        async def get(self, *_args: object, **_kwargs: object) -> None:
            return None

    payload = await system_settings.read(FakeSession())

    assert payload["legacy_kali_runner"] is False
    assert payload["runner"]["reachable"] is True


def _settings(legacy: bool) -> SimpleNamespace:
    """A stand-in for the settings object, carrying only the fields read here."""
    return SimpleNamespace(
        legacy_kali_runner=legacy,
        runner_url="http://192.168.236.128:8091",
        runner_api_token="t",
        allowed_service_cidrs="127.0.0.0/8",
        default_model_config_id=None,
    )


def test_the_shipped_default_is_off() -> None:
    """A new deployment must not inherit the retired dependency.

    Reads the real configuration, so a stray ``APP_LEGACY_KALI_RUNNER=1`` in the
    working environment is caught rather than assumed away.
    """
    assert getattr(get_settings(), "legacy_kali_runner", None) is False