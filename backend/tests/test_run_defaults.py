from app.schemas.run import RunCreate, RunRead


def test_new_runs_default_to_multi_agent_v1() -> None:
    assert RunCreate().solver_mode == "multi_agent_v1"
    assert RunRead.model_fields["solver_mode"].default == "multi_agent_v1"


def test_single_agent_remains_an_explicit_compatibility_mode() -> None:
    assert RunCreate(solver_mode="single_agent").solver_mode == "single_agent"
