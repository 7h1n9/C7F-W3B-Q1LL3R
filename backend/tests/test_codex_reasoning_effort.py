from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.model_config import ModelConfigWrite
from app.solver.muteki.worker.engines.codex import CodexCliEngine
from muteki.solver.cli_driver import CodexDriver, driver_for


def test_codex_model_and_reasoning_effort_are_separate_fields() -> None:
    config = ModelConfigWrite(
        name="Codex Luna",
        provider_type="codex_cli",
        model_name="gpt-5.6-luna",
        reasoning_effort="high",
        roles=["worker"],
    )

    assert config.model_name == "gpt-5.6-luna"
    assert config.reasoning_effort == "high"


def test_codex_reasoning_effort_defaults_to_medium() -> None:
    config = ModelConfigWrite(
        name="Codex Default",
        provider_type="codex_cli",
        model_name="gpt-5.6-luna",
        roles=["worker"],
    )

    assert config.reasoning_effort == "medium"


def test_codex_rejects_a_combined_model_and_effort_value() -> None:
    with pytest.raises(ValidationError):
        ModelConfigWrite(
            name="Invalid Codex",
            provider_type="codex_cli",
            model_name="gpt-5.6-luna high",
            reasoning_effort="high",
            roles=["worker"],
        )


def test_profile_driver_injects_model_and_native_reasoning_config() -> None:
    driver = driver_for(
        {
            "engine": "codex",
            "transport": "codex_cli",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "high",
        }
    )

    argv = driver.build_execute("probe", None, web_access=False)

    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.6-luna"
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert argv.index("-c") < argv.index("exec")


def test_reasoning_config_is_not_added_to_non_codex_profiles() -> None:
    driver = driver_for(
        {
            "engine": "claude",
            "transport": "local",
            "model": "claude-worker",
            "reasoning_effort": "high",
        }
    )

    argv = driver.build_execute("probe", None, web_access=False)

    assert "model_reasoning_effort=\"high\"" not in argv


def test_codex_argv_ignores_execpolicy_rules_without_removing_sandbox_bypass() -> None:
    driver = CodexDriver()
    worker = CodexCliEngine(executable="codex")
    commands = [
        driver.build_execute("probe", None, web_access=False),
        driver.build_resume("probe", "session-1", web_access=False),
        driver._hello_argv(),
        worker._command("probe"),
    ]

    for argv in commands:
        assert "--ignore-rules" in argv
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert argv.index("--ignore-rules") > argv.index("exec")
