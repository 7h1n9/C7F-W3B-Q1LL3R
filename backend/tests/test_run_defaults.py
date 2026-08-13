from app.schemas.run import RunCreate, RunRead


def test_new_runs_default_to_muteki() -> None:
    assert RunCreate().solver_mode == "muteki"
    assert RunRead.model_fields["solver_mode"].default == "multi_agent_v1"


def test_single_agent_remains_an_explicit_compatibility_mode() -> None:
    assert RunCreate(solver_mode="single_agent").solver_mode == "single_agent"


def test_worker_engine_aliases_are_normalized_at_api_boundary() -> None:
    run = RunCreate(
        worker_engines=[
            {"engine_type": "codex-sdk"},
            {"engine_type": "openai-compatible", "model_config_id": "deepseek-worker"},
        ]
    )
    assert [item.engine_type for item in run.worker_engines] == ["codex_sdk", "openai_compatible"]


def test_legacy_solver_modes_are_not_available_for_new_runs() -> None:
    for mode in ("solver_v2", "multi_agent_v1"):
        try:
            RunCreate(solver_mode=mode)
        except ValueError:
            continue
        raise AssertionError(f"legacy mode unexpectedly accepted for new run: {mode}")
