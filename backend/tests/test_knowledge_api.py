import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import knowledge as knowledge_api
from app.api.v1.knowledge import router
from app.core.exceptions import DomainError, domain_error_handler


async def _no_db_pairs(_session):
    """Keep the filesystem assertions independent of whatever the live DB holds.

    ``GET /knowledge/experiments`` merges finished matrix directories with the
    ``db_pair`` rows a control experiment writes.  A developer database that has
    ever run one therefore answers with extra entries, which is correct
    behaviour but not what these tests are about -- they assert the directory
    reader.  Without this seam the assertions only pass on an empty database.
    """

    return []


def _knowledge_item(
    item_id: str,
    *,
    kind: str = "technique",
    title: str = "SQL injection",
    technique: str = "sql-injection",
    summary: str = "Compare controlled true and false predicates.",
    target_stack: list[str] | None = None,
    published_at: str | None = "2024-06-01",
    trust_level: str = "curated",
) -> dict:
    return {
        "id": item_id,
        "kind": kind,
        "title": title,
        "technique": technique,
        "summary": summary,
        "preconditions": ["The target is authorized for testing."],
        "target_stack": target_stack or ["web", "sqlite"],
        "payload_template": "' OR 1=1--",
        "verification_method": ["Compare a control request with the candidate request."],
        "known_failures": ["A WAF may normalize the payload."],
        "steps": ["Send a control request.", "Send a candidate request.", "Compare outputs."],
        "provenance": {
            "source_type": "standard",
            "source_ref": "OWASP test reference",
            "source_url": None,
            "notes": None,
        },
        "published_at": published_at,
        "trust_level": trust_level,
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def knowledge_root(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    payloads = [
        _knowledge_item(
            "case-sqli",
            kind="case",
            title="Boolean SQL injection case",
            technique="SQL injection",
            target_stack=["Python", "SQLite"],
            published_at="2023-01-02",
            trust_level="reviewed",
        ),
        _knowledge_item(
            "poc-xss",
            kind="poc",
            title="Reflected XSS proof",
            technique="cross-site scripting",
            summary="Test a harmless reflected browser context.",
            target_stack=["Node.js", "Browser"],
            published_at="2025-03-01",
            trust_level="unverified",
        ),
        _knowledge_item(
            "negative-auth",
            kind="negative",
            title="JWT algorithm confusion negative result",
            technique="jwt",
            summary="The target rejected the candidate token transformation.",
            target_stack=["Java"],
            published_at=None,
            trust_level="curated",
        ),
    ]
    with (root / "items.jsonl").open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload) + "\n")
    monkeypatch.setenv("C7F_KNOWLEDGE_ROOT", str(root))
    return root


def test_items_support_search_filters_paging_and_stable_sort(
    client: TestClient,
    knowledge_root,
) -> None:
    response = client.get(
        "/api/v1/knowledge/items",
        params={
            "query": "sql",
            "kind": "case",
            "trust_level": "reviewed",
            "target_stack": "sqlite",
            "limit": 1,
            "offset": 0,
        },
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == ["case-sqli"]

    first_page = client.get(
        "/api/v1/knowledge/items",
        params={"limit": 2, "offset": 0},
    ).json()["data"]
    second_page = client.get(
        "/api/v1/knowledge/items",
        params={"limit": 2, "offset": 2},
    ).json()["data"]
    assert [item["id"] for item in first_page["items"]] == [
        "case-sqli",
        "negative-auth",
    ]
    assert [item["id"] for item in second_page["items"]] == ["poc-xss"]
    assert first_page["total"] == second_page["total"] == 3


def test_stats_use_strict_2025_cutoff_and_report_date_bounds(
    client: TestClient,
    knowledge_root,
) -> None:
    response = client.get("/api/v1/knowledge/stats")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body == {
        "total": 3,
        "by_kind": {"case": 1, "negative": 1, "poc": 1},
        "by_trust_level": {"curated": 1, "reviewed": 1, "unverified": 1},
        "published_before_2025_count": 1,
        "earliest_published_at": "2023-01-02",
        "latest_published_at": "2025-03-01",
        "root": str(knowledge_root),
    }


def test_preview_returns_scored_prior_with_explicit_unverified_warning(
    client: TestClient,
    knowledge_root,
) -> None:
    response = client.post(
        "/api/v1/knowledge/preview",
        json={
            "query": "boolean sql injection sqlite",
            "metadata": {"kind": "case", "target_stack": "sqlite"},
            "top_k": 2,
        },
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["query"] == "boolean sql injection sqlite"
    assert body["metadata"] == {"kind": "case", "target_stack": "sqlite"}
    assert body["top_k"] == 2
    assert body["results"]
    assert body["results"][0]["item"]["id"] == "case-sqli"
    assert body["results"][0]["score"] > 0
    assert "待验证先验" in body["formatted_prior"]
    assert "VerifiedFact" in body["formatted_prior"]
    assert "not VerifiedFact" in body["warning"]


def test_experiment_list_is_empty_when_root_does_not_exist(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "missing-experiments"
    monkeypatch.setenv("C7F_KNOWLEDGE_EXPERIMENT_ROOT", str(root))
    monkeypatch.setattr(knowledge_api, "_read_db_experiment_pairs", _no_db_pairs)

    response = client.get("/api/v1/knowledge/experiments")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "experiments": [],
        "root": str(root.resolve()),
    }


def test_experiment_detail_reads_metadata_and_bounded_artifacts(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "experiments"
    experiment = root / "exp-001"
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True)
    (experiment / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": "2025-01-01T00:00:00+00:00",
                "cases": [{"case_id": "case-a"}, {"case_id": "case-b"}],
            }
        ),
        encoding="utf-8",
    )
    (experiment / "plan.json").write_text(
        json.dumps(
            {
                "created_at": "2025-01-01T00:00:01+00:00",
                "dry_run": False,
                "tasks": [
                    {"case_id": "case-a", "arm": "off", "repeat": 1},
                    {"case_id": "case-a", "arm": "on", "repeat": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    (experiment / "summary.json").write_text(
        json.dumps(
            {
                "record_count": 2,
                "paired_effect": {
                    "sample_count": 1,
                    "sample_adequate": False,
                    "warning": "descriptive_only",
                },
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "case-a__on__r1.json").write_text(
        json.dumps({"case_id": "case-a", "arm": "on", "success": True}),
        encoding="utf-8",
    )
    monkeypatch.setenv("C7F_KNOWLEDGE_EXPERIMENT_ROOT", str(root))
    monkeypatch.setattr(knowledge_api, "_read_db_experiment_pairs", _no_db_pairs)

    listing = client.get("/api/v1/knowledge/experiments")
    detail = client.get("/api/v1/knowledge/experiments/exp-001")

    assert listing.status_code == 200
    listed = listing.json()["data"]["experiments"]
    assert len(listed) == 1
    assert listed[0]["id"] == "exp-001"
    assert listed[0]["status"] == "completed"
    assert listed[0]["case_count"] == 2
    assert listed[0]["planned_jobs"] == 2
    assert listed[0]["record_count"] == 2
    assert listed[0]["paired_effect"]["warning"] == "descriptive_only"
    assert detail.status_code == 200
    detail_body = detail.json()["data"]
    assert detail_body["manifest"]["cases"][0]["case_id"] == "case-a"
    assert detail_body["plan"]["tasks"][0]["arm"] == "off"
    assert detail_body["summary"]["record_count"] == 2
    assert detail_body["minimum_paired_samples"] == 10
    assert detail_body["artifact_count"] == 1
    assert detail_body["artifacts"][0]["data"]["success"] is True


def test_experiment_detail_rejects_unsafe_ids_and_missing_experiments(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "experiments"
    root.mkdir()
    monkeypatch.setenv("C7F_KNOWLEDGE_EXPERIMENT_ROOT", str(root))

    unsafe = client.get("/api/v1/knowledge/experiments/bad..id")
    missing = client.get("/api/v1/knowledge/experiments/missing")

    assert unsafe.status_code == 404
    assert unsafe.json()["code"] == "KNOWLEDGE_EXPERIMENT_NOT_FOUND"
    assert missing.status_code == 404
    assert missing.json()["code"] == "KNOWLEDGE_EXPERIMENT_NOT_FOUND"
