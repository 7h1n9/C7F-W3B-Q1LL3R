from app.solver.muteki.runtime.configuration import (
    WorkerEngineSelection,
    normalize_worker_engines,
    runtime_selection_from_hints,
    write_runtime_selection,
)
from app.solver.muteki.runtime.muteki_runtime import (
    MutekiRuntime,
    _resolve_official_account_root,
    resolve_muteki_worker_backend,
)


def test_production_muteki_backend_is_container_only(monkeypatch) -> None:
    monkeypatch.delenv("APP_MUTEKI_WORKER_BACKEND", raising=False)
    assert resolve_muteki_worker_backend(None) == "upstream_container"
    assert resolve_muteki_worker_backend("upstream_container") == "upstream_container"
    assert resolve_muteki_worker_backend("upstream_local") == "upstream_local"


def test_production_muteki_rejects_legacy_runner_backends() -> None:
    for backend in ("gateway", "callback", "cli", "container"):
        try:
            resolve_muteki_worker_backend(backend)
        except ValueError as error:
            assert "MUTEKI_CONTAINER_ONLY" in str(error)
        else:  # pragma: no cover - assertion makes an accidental fallback visible
            raise AssertionError(f"legacy backend unexpectedly accepted: {backend}")


def test_worker_selection_keeps_official_codex_identity() -> None:
    selection = WorkerEngineSelection("codex_cli", "codex-config")
    assert selection.engine_id == "codex-cli:codex-config"
    assert selection.to_dict() == {"engine_type": "codex_cli", "engine_id": "codex-cli:codex-config", "model_config_id": "codex-config"}


def test_worker_selection_accepts_legacy_codex_alias_without_changing_identity() -> None:
    selection = WorkerEngineSelection("codex-cli", "alias-config")
    assert selection.engine_type == "codex_cli"
    assert selection.engine_id == "codex-cli:alias-config"


def test_worker_selection_deduplicates_without_downgrading_openai_config() -> None:
    selections = normalize_worker_engines(
        [
            {"engine_type": "codex_cli", "model_config_id": "codex-main"},
            {"engine_type": "openai_compatible", "model_config_id": "step-config"},
            {"engine_type": "openai_compatible", "model_config_id": "step-config"},
        ]
    )
    assert [item.engine_id for item in selections] == ["codex-cli:codex-main", "openai-compatible:step-config"]


def test_runtime_selection_round_trips_through_existing_hints() -> None:
    hints = write_runtime_selection(
        {"classification": "IDOR"},
        reason_model_config_id="deepseek-reason",
        worker_engines=(WorkerEngineSelection("codex_cli", "codex-main"), WorkerEngineSelection("openai_compatible", "step-config")),
    )
    reason_id, workers = runtime_selection_from_hints(hints)
    assert reason_id == "deepseek-reason"
    assert [item.engine_type for item in workers] == ["codex_cli", "openai_compatible"]
    assert hints["classification"] == "IDOR"


def test_legacy_run_defaults_to_one_worker_engine() -> None:
    reason_id, workers = runtime_selection_from_hints({}, fallback_engine_type="openai_compatible", fallback_model_config_id="step")
    assert reason_id is None
    assert len(workers) == 1
    assert workers[0].engine_id == "openai-compatible:step"


def test_multiple_worker_profiles_remain_independent() -> None:
    selections = normalize_worker_engines(
        [
            {"engine_type": "codex-cli", "model_config_id": "cx-main"},
            {"engine_type": "openai-compatible", "model_config_id": "deepseek-worker"},
        ]
    )
    assert [item.engine_id for item in selections] == [
        "codex-cli:cx-main",
        "openai-compatible:deepseek-worker",
    ]


def test_official_account_root_uses_explicit_value_first(tmp_path) -> None:
    workspace = tmp_path / "data" / "workspaces" / "run"
    workspace.mkdir(parents=True)
    explicit = tmp_path / "accounts"
    explicit.mkdir()

    assert _resolve_official_account_root(str(workspace), explicit_root=str(explicit)) == str(explicit.resolve())


def test_official_account_root_derives_standard_sessions_store(tmp_path) -> None:
    workspace = tmp_path / "data" / "workspaces" / "run"
    workspace.mkdir(parents=True)
    account_root = tmp_path / "data" / "sessions" / "_secrets" / "accounts"
    account_root.mkdir(parents=True)

    assert _resolve_official_account_root(str(workspace)) == str(account_root.resolve())


def test_muteki_runtime_uses_challenge_schema_metadata_normalization(tmp_path) -> None:
    from types import SimpleNamespace

    workspace = tmp_path / "data" / "workspaces" / "run"
    workspace.mkdir(parents=True)
    run = SimpleNamespace(id="run-1", workspace_path=str(workspace))
    challenge = SimpleNamespace(
        id="challenge-1",
        name="Asset Warranty",
        description="Authorized asset warranty SQL challenge",
        challenge_type="WEB_TARGET",
        target_url="http://target.test",
        allowed_hosts=["target.test"],
        flag_pattern=r"flag\{[^}]+\}",
        source_path=None,
        metadata_json={
            "vulnerability_type": "SQL_INJECTION",
            "endpoint": "/api/warranty/check",
            "method": "POST",
        },
    )

    runtime = MutekiRuntime(None, run, challenge)

    assert runtime.challenge.metadata_json["adapter"] == "asset_warranty"
    assert runtime.challenge.metadata_json["fields"] == ["asset_no", "department"]
