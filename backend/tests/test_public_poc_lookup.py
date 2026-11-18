"""Public CVE / PoC lookup: bounded, read-only, and never treated as evidence.

Two properties are load-bearing and are what these tests pin:

1. The lookup is *outside* the challenge's trust boundary.  It runs on the host,
   against two fixed API hosts, and returns a summary -- so it cannot become a
   channel for fetching a payload or reaching an unlisted host.
2. A hit is a hypothesis, not a finding.  The fact it publishes is
   ``verified=False``; nothing here can satisfy ``submit_flag``'s requirement
   that the flag came out of a real observation.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solver.muteki.worker import public_poc
from app.solver.muteki.worker.openai_compatible import OpenAICompatibleWorker
from muteki.core.cost import CostController

_NVD_PAYLOAD = {
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2019-0230",
                "published": "2020-09-14T00:00:00.000",
                "descriptions": [
                    {"lang": "es", "value": "texto"},
                    {
                        "lang": "en",
                        "value": "Apache Struts 2.0.0 to 2.5.20 forced double OGNL "
                        "evaluation may lead to remote code execution.",
                    },
                ],
                "metrics": {
                    "cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]
                },
                "references": [
                    {"url": "https://example.test/s2-059"},
                    {"url": "https://example.test/s2-059b"},
                ],
            }
        }
    ]
}

_GITHUB_PAYLOAD = {
    "items": [
        {
            "full_name": "attacker/struts-poc",
            "stargazers_count": 42,
            "description": "PoC for the OGNL issue",
            "html_url": "https://github.test/attacker/struts-poc",
        }
    ]
}

#: A broad keyword: NVD answers with the *oldest* records and reports how many
#: matched.  ``tomcat`` really behaves like this (421 matches, first page 2000-era).
_PAGED_PAYLOAD = {
    "totalResults": 421,
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2000-0672",
                "published": "2000-10-20T00:00:00.000",
                "descriptions": [{"lang": "en", "value": "Ancient Tomcat issue."}],
                "metrics": {"cvssMetricV2": [{"cvssData": {"baseScore": 5.0}}]},
                "references": [],
            }
        }
    ],
}

#: The last page of that same result set -- the records a live target may run.
_TAIL_PAYLOAD = {
    "totalResults": 421,
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2026-76183",
                "published": "2026-09-23T00:00:00.000",
                "descriptions": [{"lang": "en", "value": "Authentication bypass in Apache Tomcat."}],
                "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]},
                "references": [{"url": "https://example.test/76183"}],
            }
        }
    ],
}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for ``httpx.AsyncClient``; records the hosts it was asked for."""

    calls: list[tuple[str, dict]] = []
    nvd_status = 200
    github_status = 200
    #: Answer for the first page.  Defaults to the single-result document so the
    #: narrow-keyword tests are unaffected.
    nvd_first: dict = _NVD_PAYLOAD
    #: Answer for the "last page" request.  Only reached when the match count
    #: exceeds one page.
    nvd_tail: dict = _TAIL_PAYLOAD

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None, **_kwargs):
        params = dict(params or {})
        type(self).calls.append((url, params))
        if url == public_poc.NVD_ENDPOINT:
            if "startIndex" in params:
                return _FakeResponse(type(self).nvd_status, type(self).nvd_tail)
            return _FakeResponse(type(self).nvd_status, type(self).nvd_first)
        if url == public_poc.GITHUB_SEARCH:
            return _FakeResponse(type(self).github_status, _GITHUB_PAYLOAD)
        raise AssertionError(f"unexpected host contacted: {url}")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.nvd_status = 200
    _FakeClient.github_status = 200
    _FakeClient.nvd_first = _NVD_PAYLOAD
    _FakeClient.nvd_tail = _TAIL_PAYLOAD
    monkeypatch.setattr(public_poc.httpx, "AsyncClient", _FakeClient)
    monkeypatch.delenv("C7F_PUBLIC_POC_LOOKUP", raising=False)
    public_poc._CACHE._items.clear()
    # The NVD slot queue is process-wide; without resetting it every test would
    # pay the previous test's spacing.
    public_poc._nvd_next_slot[0] = 0.0
    yield


# --- the pure lookup -------------------------------------------------------


def test_a_keyword_lookup_returns_cve_metadata_and_repositories() -> None:
    result = asyncio.run(public_poc.lookup_public_poc(keyword="apache struts 2.5.20"))
    assert result["success"] is True
    assert result["cves"][0]["cve_id"] == "CVE-2019-0230"
    assert result["cves"][0]["cvss"] == 9.8
    assert result["cves"][0]["severity"] == "CRITICAL"
    # The English description wins over the first one in the list.
    assert "double OGNL" in result["cves"][0]["summary"]
    assert result["poc_repositories"][0]["repo"] == "attacker/struts-poc"
    assert "hypothesis" in result["usage"]


def test_a_cve_id_in_the_keyword_is_detected_and_used_exactly() -> None:
    asyncio.run(public_poc.lookup_public_poc(keyword="exploit cve-2021-44228 please"))
    nvd_call = next(params for url, params in _FakeClient.calls if url == public_poc.NVD_ENDPOINT)
    assert nvd_call["cveId"] == "CVE-2021-44228"
    assert "keywordSearch" not in nvd_call


def test_only_the_two_declared_hosts_are_contacted() -> None:
    asyncio.run(public_poc.lookup_public_poc(keyword="anything"))
    hosts = {(urlsplit(url).hostname or "") for url, _params in _FakeClient.calls}
    assert hosts and hosts <= set(public_poc.ALLOWED_HOSTS)


def test_an_undeclared_host_is_refused_by_the_transport_guard() -> None:
    asyncio.run(public_poc.lookup_public_poc(keyword="x"))
    # The guard is what makes ALLOWED_HOSTS a constraint rather than a comment.
    with pytest.raises(RuntimeError, match="HOST_NOT_ALLOWED"):
        asyncio.run(public_poc._get(_FakeClient(), "https://evil.test/steal"))


def test_a_repeat_lookup_is_served_from_cache() -> None:
    first = asyncio.run(public_poc.lookup_public_poc(keyword="struts 2.5.20"))
    calls_after_first = len(_FakeClient.calls)
    second = asyncio.run(public_poc.lookup_public_poc(keyword="struts 2.5.20"))
    assert second.get("cached") is True
    assert len(_FakeClient.calls) == calls_after_first
    assert second["cves"] == first["cves"]


def test_an_nvd_failure_is_reported_not_raised() -> None:
    _FakeClient.nvd_status = 503
    result = asyncio.run(public_poc.lookup_public_poc(keyword="struts"))
    assert result["success"] is False
    assert result["error"] == "NVD_UNAVAILABLE"


def test_a_rate_limited_github_degrades_to_no_repos() -> None:
    _FakeClient.github_status = 403
    result = asyncio.run(public_poc.lookup_public_poc(keyword="struts"))
    assert result["success"] is True
    assert result["cves"]
    assert result["poc_repositories"] == []


def test_missing_input_and_a_disabled_switch_are_distinct_errors(monkeypatch) -> None:
    assert asyncio.run(public_poc.lookup_public_poc())["error"] == "KEYWORD_REQUIRED"
    monkeypatch.setenv("C7F_PUBLIC_POC_LOOKUP", "off")
    assert asyncio.run(public_poc.lookup_public_poc(keyword="x"))["error"] == "LOOKUP_DISABLED"
    assert _FakeClient.calls == []


def test_a_broad_keyword_is_answered_from_the_newest_page() -> None:
    """NVD orders ascending, so page one of a broad keyword is the oldest corpus.

    Answering a live target with a 2000-era CVE is worse than answering nothing:
    the model tests it, fails, and rules out a class that is still exploitable
    today.  The last page is fetched instead.
    """

    _FakeClient.nvd_first = _PAGED_PAYLOAD
    result = asyncio.run(public_poc.lookup_public_poc(keyword="tomcat", limit=4))

    tail_calls = [params for url, params in _FakeClient.calls if params.get("startIndex")]
    assert tail_calls, "a multi-page result must be re-fetched from the tail"
    assert tail_calls[0]["startIndex"] == 421 - public_poc._NVD_PAGE_SIZE
    assert [cve["cve_id"] for cve in result["cves"]] == ["CVE-2026-76183"]
    assert result["total_results"] == 421
    assert "421 CVEs" in (result.get("hint") or "")


def test_a_narrow_keyword_does_not_request_a_second_page() -> None:
    asyncio.run(public_poc.lookup_public_poc(keyword="apache struts 2.5.20"))
    assert not [params for _url, params in _FakeClient.calls if "startIndex" in params]


def test_the_best_scoring_record_wins_within_a_page() -> None:
    """NVD's order inside a page is arbitrary; the model reads the top one."""

    low = {
        "cve": {
            "id": "CVE-2020-0001",
            "published": "2020-01-01T00:00:00.000",
            "descriptions": [{"lang": "en", "value": "Low."}],
            "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 4.3}}]},
            "references": [],
        }
    }
    high = _TAIL_PAYLOAD["vulnerabilities"][0]
    _FakeClient.nvd_first = {"totalResults": 2, "vulnerabilities": [low, high]}

    result = asyncio.run(public_poc.lookup_public_poc(keyword="product"))
    assert [cve["cve_id"] for cve in result["cves"]] == ["CVE-2026-76183", "CVE-2020-0001"]


def test_a_sentence_that_matches_nothing_suggests_a_narrower_keyword() -> None:
    """``keywordSearch`` is an AND, so a pasted sentence returns zero results.

    The failure is the model's phrasing, not an absence of CVEs, and it is
    cheaper to say what to type than to spend another Worker turn guessing.
    """

    _FakeClient.nvd_first = {"totalResults": 0, "vulnerabilities": []}
    # GitHub rate-limited (60 req/h unauthenticated), so there is no technique
    # fallback either: this is the "truly nothing" case.
    _FakeClient.github_status = 403
    result = asyncio.run(
        public_poc.lookup_public_poc(keyword="apache tomcat 8.5.19 put jsp arbitrary file write")
    )

    assert result["found"] is False
    assert result["cves"] == []
    assert "tomcat 8.5.19" in result["suggested_keywords"]
    assert "Retry with one of" in (result.get("hint") or "")


def test_technique_lookup_returns_harness_repositories_without_a_cve() -> None:
    """A post-exploitation question has no CVE, and its answer is the harness.

    ``www-data`` on a Debian container is not a CVE.  Before this, a lookup for
    "linux privilege escalation 4.19" resolved no CVE and reported ``found:
    False`` even though the technique is extremely well documented -- the Worker
    read that as "this avenue is closed" and stopped.
    """

    _FakeClient.nvd_first = {"totalResults": 0, "vulnerabilities": []}
    result = asyncio.run(
        public_poc.lookup_public_poc(keyword="linux privilege escalation 4.19")
    )

    assert result["cves"] == []
    assert result["found"] is True
    assert result["poc_repositories"]
    # The repository query is technique-shaped, not CVE-shaped.
    github_calls = [params for url, params in _FakeClient.calls if url == public_poc.GITHUB_SEARCH]
    assert github_calls
    assert "linux privilege escalation" in str(github_calls[-1]["q"])
    assert "exploit" in str(github_calls[-1]["q"])
    assert "repositories implement the technique" in (result.get("hint") or "")


def test_keyword_variants_drop_the_version_that_nvd_cannot_match() -> None:
    """NVD keeps version ranges in CPE, so an exact version usually matches nothing.

    ``tomcat 8.5.19`` returns zero records while ``tomcat`` returns 421.  If the
    retry keeps the version it repeats the same empty query, and the model
    concludes the product has no known CVE.
    """

    variants = public_poc._keyword_variants("apache tomcat 8.5.19")
    assert variants[0] == "apache tomcat 8.5.19"
    assert "apache tomcat" in variants


def test_keyword_variants_stop_at_three_queries() -> None:
    variants = public_poc._keyword_variants("apache tomcat allows an attacker to read files")
    assert variants[0] == "apache tomcat allows an attacker to read files"
    assert variants[1] == "apache tomcat attacker read"
    assert len(variants) <= 3, "the NVD request budget is three keyword queries"


# --- the Worker action -----------------------------------------------------


class _Graph:
    def __init__(self) -> None:
        self.facts: list[dict] = []

    def snapshot(self):
        return SimpleNamespace(evidence=[])

    def add_evidence(self, *, fact: str, verified: bool = True, **_kwargs) -> int:
        self.facts.append({"fact": json.loads(fact), "verified": verified})
        return len(self.facts)


def _worker(tmp_path: Path, graph: _Graph) -> OpenAICompatibleWorker:
    return OpenAICompatibleWorker(
        graph=graph,
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-lookup",
        intent_id="intent-lookup",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret",
        model="deepseek-v4-flash",
        max_turns=2,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run-lookup",
    )


def test_lookup_is_a_declared_worker_tool() -> None:
    from app.solver.muteki.worker.openai_compatible import WORKER_TOOLS

    names = {tool["function"]["name"] for tool in WORKER_TOOLS}
    assert "lookup_public_poc" in names


def test_the_action_publishes_an_unverified_fact(tmp_path: Path) -> None:
    graph = _Graph()
    worker = _worker(tmp_path, graph)
    result = asyncio.run(worker._lookup_public_poc({"keyword": "apache struts 2.5.20"}))
    assert result["success"] is True
    assert result["lookups_used"] == 1
    assert graph.facts, "the lookup must be visible to sibling Workers"
    published = graph.facts[0]
    assert published["verified"] is False
    assert published["fact"]["tool"] == "lookup_public_poc"
    assert published["fact"]["cve_ids"] == ["CVE-2019-0230"]


def test_the_action_refuses_to_start_without_a_query(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _Graph())
    result = asyncio.run(worker._lookup_public_poc({}))
    assert result["error"] == "KEYWORD_OR_CVE_ID_REQUIRED"


def test_the_action_budget_is_enforced(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _Graph())
    for index in range(4):
        result = asyncio.run(worker._lookup_public_poc({"keyword": f"product-{index}"}))
        assert result["success"] is True
    exhausted = asyncio.run(worker._lookup_public_poc({"keyword": "one-more"}))
    assert exhausted["error"] == "LOOKUP_BUDGET_EXHAUSTED"
