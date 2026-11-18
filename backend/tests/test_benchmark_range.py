"""The range endpoint resolver and the shipped case targets must agree.

A case target that still contains ``{{host}}`` / ``{{port}}`` after resolution
sends the agent at a literal placeholder, and a target naming a port the range
does not publish makes every repetition fail for a reason unrelated to the
solver.  These tests pin both halves: the substitution, and the fact that the
two runnable SQL-injection cases actually describe the shipped range.
"""

from __future__ import annotations

import json
import socket

import pytest

from app.benchmark.case_loader import DEFAULT_CASE_DIR, load_case, load_cases
from app.benchmark.experiment import (
    ExecutionResult,
    ExperimentRequest,
    ExperimentRunner,
    _case_runnable,
)
from app.benchmark.range import (
    DEFAULT_RANGE_NAME,
    FLAG_ENV_KEY,
    PORT_ENV_KEY,
    RangeEndpoint,
    available_range,
    generate_flag,
    pick_free_port,
    range_endpoint,
    read_env_values,
    resolve_case_target,
    write_env_values,
)


class FakeExecutor:
    """Dry runs never call the executor; this only satisfies the protocol."""

    async def execute(self, request: ExperimentRequest) -> ExecutionResult:
        raise AssertionError("a dry run must not execute anything")


def test_resolve_substitutes_host_and_port():
    endpoint = RangeEndpoint(host="127.0.0.1", port=8383)
    assert (
        resolve_case_target("http://{{host}}:{{port}}", endpoint)
        == "http://127.0.0.1:8383"
    )


def test_resolve_rejects_an_unknown_placeholder():
    # A silent pass-through would ship a literal {{...}} into a Run.
    with pytest.raises(ValueError, match="unknown range placeholder"):
        resolve_case_target("http://{{host}}:{{nope}}", RangeEndpoint("127.0.0.1", 1))


def test_env_rejects_a_blank_host(monkeypatch):
    # The environment is the operator-facing trust boundary, so that is where
    # the host is validated; a directly constructed RangeEndpoint is internal.
    monkeypatch.setenv("C7F_RANGE_HOST", "   ")
    with pytest.raises(ValueError, match="C7F_RANGE_HOST"):
        range_endpoint()


def test_env_overrides_the_range_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("C7F_RANGE_PORT", "12345")
    monkeypatch.setattr(
        "app.benchmark.range.DEFAULT_RANGE_DIR", tmp_path, raising=True
    )
    assert range_endpoint().port == 12345


def test_endpoint_falls_back_to_the_range_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("C7F_RANGE_PORT", raising=False)
    monkeypatch.setattr(
        "app.benchmark.range.DEFAULT_RANGE_DIR", tmp_path, raising=True
    )
    write_env_values(tmp_path / ".env", {PORT_ENV_KEY: "45678"})
    # compose resolves the published port from this file, so it has to win over
    # the built-in default or a randomised run would resolve to a stale port.
    assert range_endpoint().port == 45678


def test_endpoint_rejects_a_non_numeric_port(monkeypatch, tmp_path):
    monkeypatch.delenv("C7F_RANGE_PORT", raising=False)
    monkeypatch.setattr(
        "app.benchmark.range.DEFAULT_RANGE_DIR", tmp_path, raising=True
    )
    write_env_values(tmp_path / ".env", {PORT_ENV_KEY: "not-a-port"})
    with pytest.raises(ValueError, match="must be an integer"):
        range_endpoint()


def test_write_env_values_updates_in_place_and_preserves_other_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MYSQL_ROOT_PASSWORD=keep-me\n"
        f"{FLAG_ENV_KEY}=flag{{old}}\n"
        "# a comment\n",
        encoding="utf-8",
    )
    write_env_values(env_path, {FLAG_ENV_KEY: "flag{new}", PORT_ENV_KEY: "28037"})

    values = read_env_values(env_path)
    assert values["MYSQL_ROOT_PASSWORD"] == "keep-me"
    assert values[FLAG_ENV_KEY] == "flag{new}"
    assert values[PORT_ENV_KEY] == "28037"
    assert "# a comment" in env_path.read_text(encoding="utf-8")


def test_read_env_values_ignores_comments_and_blank_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("\n# commentary\nA=1\n\nB = 2 \n", encoding="utf-8")
    assert read_env_values(env_path) == {"A": "1", "B": "2"}
    assert read_env_values(tmp_path / "missing.env") == {}


def test_generate_flag_matches_the_range_shape():
    flag = generate_flag()
    assert flag.startswith("flag{") and flag.endswith("}")
    assert len(flag) == len("flag{") + 32 + 1
    assert flag != generate_flag()


def test_pick_free_port_returns_a_bindable_port():
    port = pick_free_port()
    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


@pytest.mark.parametrize(
    "case_id", ["sql-injection-golden", "sql_injection_boolean"]
)
def test_runnable_cases_target_the_shipped_range(case_id):
    case = load_case(case_id)
    resolved = resolve_case_target(case.target, RangeEndpoint("127.0.0.1", 28037))

    assert "{{" not in resolved
    assert resolved == "http://127.0.0.1:28037"
    # The range implements exactly one injection point; a case naming another
    # parameter cannot be scored against it.
    surfaces = case.attack_surface
    assert [entry["parameter"] for entry in surfaces] == ["department"]
    assert [entry["endpoint"] for entry in surfaces] == ["/api/warranty/check"]


def test_available_range_defaults_to_the_shipped_one(monkeypatch):
    monkeypatch.delenv("C7F_RANGE_NAME", raising=False)
    assert available_range() == DEFAULT_RANGE_NAME
    monkeypatch.setenv("C7F_RANGE_NAME", "elsewhere")
    assert available_range() == "elsewhere"
    monkeypatch.setenv("C7F_RANGE_NAME", "   ")
    assert available_range() == DEFAULT_RANGE_NAME


def test_cases_needing_an_absent_range_are_not_runnable():
    available = available_range()
    ssrf = load_case("ssrf-internal")
    xss = load_case("xss-reflected")

    # Both describe endpoints (/fetch?url= and /search?q=) that no range in this
    # repo implements, so they name a range that does not exist.
    assert ssrf.requires_range == "web-vuln-lab"
    assert xss.requires_range == "web-vuln-lab"
    assert not _case_runnable(ssrf, available)
    assert not _case_runnable(xss, available)
    assert _case_runnable(load_case("sql-injection-golden"), available)


@pytest.mark.asyncio
async def test_a_dry_run_skips_unservable_cases_and_says_so(tmp_path):
    runner = ExperimentRunner(
        case_dir=DEFAULT_CASE_DIR,
        output_dir=tmp_path / "experiment",
        executor=FakeExecutor(),
        dry_run=True,
    )

    summary = await runner.run()

    # Two of the shipped cases cannot run: they must be reported as skipped
    # rather than averaged in as failures.
    assert summary["skipped_case_count"] == 2
    assert {entry["case_id"] for entry in summary["skipped_cases"]} == {
        "ssrf-internal",
        "xss-reflected",
    }
    assert {entry["status"] for entry in summary["skipped_cases"]} == {
        "SKIPPED_REQUIRES_RANGE_MISSING"
    }
    assert summary["record_count"] == 0

    plan = json.loads(
        (tmp_path / "experiment" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan["skipped_cases"] == summary["skipped_cases"]
    # Every runnable case runs both arms, and nothing runs for a skipped one.
    # Derived rather than hardcoded: the literal 4 became 6 the moment a fifth
    # case shipped, and a count is not the property worth pinning here.
    skipped = {entry["case_id"] for entry in summary["skipped_cases"]}
    runnable = {case.case_id for case in load_cases()} - skipped
    assert {task["case_id"] for task in plan["tasks"]} == runnable
    assert len(plan["tasks"]) == len(runnable) * 2
    assert {task["arm"] for task in plan["tasks"]} == {"on", "off"}


@pytest.mark.asyncio
async def test_a_pinned_available_range_lets_the_cases_through(tmp_path, monkeypatch):
    monkeypatch.setenv("C7F_RANGE_NAME", "web-vuln-lab")
    runner = ExperimentRunner(
        case_dir=DEFAULT_CASE_DIR,
        output_dir=tmp_path / "experiment",
        executor=FakeExecutor(),
        dry_run=True,
    )

    summary = await runner.run()

    assert summary["skipped_case_count"] == 0
    assert summary["skipped_cases"] == []
