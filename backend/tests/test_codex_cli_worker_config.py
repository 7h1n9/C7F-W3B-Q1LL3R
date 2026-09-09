from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.model_config import ModelConfigWrite
from app.solver.muteki.runtime.configuration import WorkerEngineSelection


def test_codex_cli_local_model_config_needs_no_api_url() -> None:
    config = ModelConfigWrite(
        name="本机 Codex",
        provider_type="codex_cli",
        model_name="gpt-5-codex",
        roles=["worker"],
    )
    assert config.base_url is None
    assert config.api_key is None
    assert config.wire_api is None


def test_codex_cli_api_model_config_uses_official_responses_endpoint() -> None:
    config = ModelConfigWrite(
        name="Codex API",
        provider_type="codex_cli",
        base_url="https://api.openai.com/v1",
        model_name="gpt-5-codex",
        api_key="sk-test",
        roles=["worker"],
    )
    assert config.base_url is not None
    assert config.api_key == "sk-test"
    assert config.wire_api == "responses"


def test_codex_cli_api_model_requires_api_key() -> None:
    with pytest.raises(ValidationError, match="api_key"):
        ModelConfigWrite(
            name="缺少 Key 的 Codex API",
            provider_type="codex_cli",
            base_url="https://api.openai.com/v1",
            model_name="gpt-5-codex",
            roles=["worker"],
        )


def test_codex_cli_model_config_rejects_coordinator_role() -> None:
    with pytest.raises(ValidationError, match="Worker-only"):
        ModelConfigWrite(
            name="错误配置",
            provider_type="codex_cli",
            model_name="gpt-5-codex",
            roles=["coordinator_reason"],
        )


def test_codex_cli_worker_selection_keeps_distinct_identity() -> None:
    selection = WorkerEngineSelection("codex-cli", "codex-config")
    assert selection.engine_type == "codex_cli"
    assert selection.engine_id == "codex-cli:codex-config"
    assert selection.to_dict() == {
        "engine_type": "codex_cli",
        "engine_id": "codex-cli:codex-config",
        "model_config_id": "codex-config",
    }


def test_codex_cli_worker_cannot_silently_use_mock_or_openai_contract() -> None:
    with pytest.raises(ValueError, match="codex_cli worker requires model_config_id"):
        WorkerEngineSelection("codex_cli")
    with pytest.raises(ValidationError, match="base_url"):
        ModelConfigWrite(
            name="缺少 URL 的 API 模型",
            provider_type="openai_compatible",
            model_name="step-1",
        )
