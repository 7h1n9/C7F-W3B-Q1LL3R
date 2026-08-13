from __future__ import annotations

from pathlib import Path

from muteki.solver.credential_accounts import project_account_root


def test_codex_projection_ignores_transient_runtime_tree(tmp_path: Path) -> None:
    source = tmp_path / "accounts"
    codex_home = source / "codex-main" / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    (codex_home / "config.toml").write_text("model = 'gpt-5-codex'\n", encoding="utf-8")
    # This mirrors the stale/deep state that previously made copytree fail.
    transient = codex_home / ".tmp" / "plugins" / "nested" / "broken"
    transient.mkdir(parents=True)
    (transient / "not-needed.txt").write_text("cache", encoding="utf-8")

    destination = project_account_root(source, tmp_path / "projection")
    projected_home = destination / "codex-main" / "codex-home"

    assert (projected_home / "auth.json").read_text(encoding="utf-8") == '{"tokens": {}}'
    assert (projected_home / "config.toml").exists()
    assert not (projected_home / ".tmp").exists()


def test_codex_projection_succeeds_with_auth_only(tmp_path: Path) -> None:
    source = tmp_path / "accounts"
    codex_home = source / "codex-main" / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")

    destination = project_account_root(source, tmp_path / "projection")

    assert (destination / "codex-main" / "codex-home" / "auth.json").is_file()


def test_projection_preserves_non_codex_account_files(tmp_path: Path) -> None:
    source = tmp_path / "accounts"
    account = source / "api-main"
    account.mkdir(parents=True)
    (account / "API_KEY").write_text("redacted-test-key", encoding="utf-8")
    (account / "BASE_URL").write_text("https://example.invalid/v1", encoding="utf-8")

    destination = project_account_root(source, tmp_path / "projection")

    assert (destination / "api-main" / "API_KEY").read_text(encoding="utf-8") == "redacted-test-key"
    assert (destination / "api-main" / "BASE_URL").read_text(encoding="utf-8") == "https://example.invalid/v1"
