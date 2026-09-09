from __future__ import annotations

from types import SimpleNamespace

from app.solver.muteki.worker.official_worker import (
    _classify_cli_health_failure,
    _credential_engine_name,
)


def test_codex_health_failure_classification_is_safe_and_actionable() -> None:
    assert _classify_cli_health_failure(
        SimpleNamespace(text="unexpected status 401 Unauthorized: Missing bearer")
    ) == "CODEX_CLI_AUTH_FAILED"
    assert _classify_cli_health_failure(
        SimpleNamespace(text="The gpt-5.6-luna model requires a newer version of Codex")
    ) == "CODEX_CLI_VERSION_UNSUPPORTED"


def test_codex_health_failure_does_not_return_provider_text() -> None:
    result = _classify_cli_health_failure(
        SimpleNamespace(raw_stderr="provider secret-token response unavailable")
    )

    assert result == "WORKER_HEALTHCHECK_FAILED"
    assert "secret-token" not in result


def test_codex_worker_identity_resolves_credentials_through_profile_engine() -> None:
    assert _credential_engine_name(
        "codex-cli:model-config-1",
        {"engine": "codex", "protocol": "codex_cli"},
    ) == "codex"
    assert _credential_engine_name("codex", None) == "codex"
