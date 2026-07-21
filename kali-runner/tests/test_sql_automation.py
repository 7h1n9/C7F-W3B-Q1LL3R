import pytest

from app.executors import sql_automation
from app.models import JobRequest


@pytest.mark.asyncio
async def test_sql_probe_is_bounded_and_reports_boolean_differential(monkeypatch) -> None:
    seen = []

    async def fake_http(request):
        value = request.arguments["query"]["id"]
        seen.append(value)
        return {"status_code": 200 if "1=2" not in value else 500, "body": "ok" if "1=2" not in value else "sql error", "body_length": 2}

    monkeypatch.setattr(sql_automation, "execute_http", fake_http)
    request = JobRequest(run_id="run", allowed_hosts=["challenge.test"], tool="sql_injection_probe", arguments={
        "endpoint": "http://challenge.test/search", "parameter": "id", "baseline_value": "1", "max_requests": 40,
    })
    result = await sql_automation.sql_injection_probe(request)
    structured = result["structured_result"]
    assert len(seen) <= 40
    assert structured["boolean_differential"] is True
    assert structured["sql_injection_confirmed"] is True


@pytest.mark.asyncio
async def test_union_probe_caps_columns_and_requests(monkeypatch) -> None:
    calls = 0

    async def fake_http(request):
        nonlocal calls
        calls += 1
        return {"status_code": 200, "body": "", "body_length": 0}

    monkeypatch.setattr(sql_automation, "execute_http", fake_http)
    request = JobRequest(run_id="run", allowed_hosts=["challenge.test"], tool="sql_union_probe", arguments={
        "endpoint": "http://challenge.test/search", "parameter": "id", "max_columns": 100, "max_requests": 1000,
    })
    result = await sql_automation.sql_union_probe(request)
    assert calls <= 40
    assert result["structured_result"]["max_columns"] == 10
    assert result["structured_result"]["full_database_extraction"] is False
