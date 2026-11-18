"""Tests for the policy-version fingerprint.

The contract: same bytes in, same id out; any strategy edit in, a different id
out; a missing optional corpus is reported, never fatal.
"""

from __future__ import annotations

from pathlib import Path

from app.training.policy import (
    OPTIONAL_SOURCES,
    POLICY_SOURCES,
    compute_policy_version,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for index, relative in enumerate(POLICY_SOURCES):
        write(root / relative, f"# source {index}\n")
    for relative in OPTIONAL_SOURCES:
        write(root / relative, "{}\n")
    return root


def test_version_is_stable_for_identical_sources(tmp_path):
    root = make_root(tmp_path)
    first = compute_policy_version(root)
    second = compute_policy_version(root)
    assert first["policy_version"] == second["policy_version"]
    assert first["policy_version"].startswith("pol-")
    assert first["missing"] == []


def test_editing_a_strategy_source_changes_the_version(tmp_path):
    root = make_root(tmp_path)
    before = compute_policy_version(root)["policy_version"]
    write(root / POLICY_SOURCES[0], "# a real strategy change\n")
    after = compute_policy_version(root)["policy_version"]
    assert before != after


def test_missing_optional_corpus_is_reported_not_fatal(tmp_path):
    root = make_root(tmp_path)
    (root / OPTIONAL_SOURCES[0]).unlink()
    result = compute_policy_version(root)
    assert OPTIONAL_SOURCES[0] in result["missing"]
    assert result["policy_version"].startswith("pol-")


def test_extra_parameters_fold_into_the_id(tmp_path):
    root = make_root(tmp_path)
    base = compute_policy_version(root)["policy_version"]
    with_arm = compute_policy_version(root, extra={"arms": "off,on"})["policy_version"]
    assert base != with_arm


def test_empty_root_yields_unknown_not_a_crash(tmp_path):
    result = compute_policy_version(tmp_path / "nowhere")
    assert result["policy_version"] == "pol-unknown"
    assert result["components"] == {}


def test_real_repository_hashes_successfully():
    # Guards against a path typo silently degrading every experiment to
    # "pol-unknown" while tests stay green.
    result = compute_policy_version()
    assert result["policy_version"] != "pol-unknown"
    assert POLICY_SOURCES[0] in result["components"]
