"""Read-only lookup of public CVE metadata and PoC repositories.

Why this lives on the host side rather than in the Worker container: the Worker
container is deliberately constrained to the challenge target, and a lookup that
needs to reach NVD and GitHub would have to widen that boundary.  Keeping the
request on the host also means the result is a *summary* rather than a page of
HTML the model can mistake for a finding.

What this deliberately does not do:

* it never downloads or executes a PoC;
* it never fetches anything outside the two API hosts below;
* it never returns a payload as an instruction.  A PoC description is evidence
  that a vulnerability class exists for a product, not proof that this target
  is vulnerable -- the Worker still has to observe the behaviour.

That distinction matters for the flag rule: nothing here can produce a flag, so
a model cannot satisfy ``submit_flag`` by quoting a CVE record.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

import httpx

NVD_ENDPOINT = "https://services.nvd.nist.gov/rest/json/cves/2.0"
GITHUB_SEARCH = "https://api.github.com/search/repositories"

#: Only these hosts are ever contacted.  Kept as a constant (not a parameter)
#: so a model cannot redirect the lookup with a crafted keyword.
ALLOWED_HOSTS = frozenset({"services.nvd.nist.gov", "api.github.com"})

_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_MAX_SUMMARY = 320
_MAX_REFS = 3
_MAX_REPOS = 3
_CACHE_LIMIT = 64

#: NVD's own ordering is by publication date *ascending*, so asking for three
#: results of a broad keyword answers with the three oldest CVEs in the corpus
#: (``tomcat`` returns CVE-2000-0672).  Fetch a page, rank locally, then cut.
_NVD_PAGE_SIZE = 50

#: ``keywordSearch`` is an AND over tokens.  A model that pastes a sentence --
#: "apache tomcat 8.5.19 put jsp arbitrary file write" -- matches nothing, so the
#: query is retried on progressively smaller cores instead of returning empty.
_STOPWORDS = frozenset(
    {
        "a", "allow", "allows", "an", "and", "are", "be", "by", "can", "could",
        "for", "from", "in", "is", "it", "leads", "may", "of", "on", "or",
        "that", "the", "their", "this", "to", "via", "when", "which", "with",
    }
)


class _LookupCache:
    """Small LRU so one Run does not re-query the same product per Worker."""

    def __init__(self, limit: int = _CACHE_LIMIT) -> None:
        self._items: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._limit = limit

    def get(self, key: tuple[str, str]) -> dict[str, Any] | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: tuple[str, str], value: dict[str, Any]) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)


_CACHE = _LookupCache()

#: NVD allows five requests per thirty seconds without an API key, and one
#: lookup can issue three (original keyword, relaxed keyword, last page) while
#: several Workers run at once.  A 403 is indistinguishable from "the product
#: has no CVE" at the call site, so the failure would look like a capability
#: limit rather than a quota.
#:
#: Each caller reserves the next slot by moving the marker forward before it
#: sleeps.  That queues concurrent callers without a lock, which matters because
#: a module-level ``asyncio.Lock`` binds to the first event loop that acquires it
#: and every Worker runs its own loop.
_NVD_MIN_INTERVAL = 1.5
_nvd_next_slot = [0.0]


def lookup_enabled() -> bool:
    """Whether outbound lookups are permitted for this deployment.

    Default on: an offline deployment loses one tool call, while a deployment
    that has to opt in would silently keep the old behaviour and be mistaken for
    "lookup was tried and found nothing".
    """

    raw = str(os.getenv("C7F_PUBLIC_POC_LOOKUP", "on")).strip().lower()
    return raw not in {"0", "off", "false", "no", "disabled"}


def _severity(metrics: dict[str, Any]) -> tuple[float | None, str]:
    """Best available CVSS score across the metric v3.1/v3.0/v2 blocks."""

    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key)
        if isinstance(entries, list) and entries:
            data = entries[0].get("cvssData") if isinstance(entries[0], dict) else None
            if isinstance(data, dict):
                score = data.get("baseScore")
                severity = str(data.get("baseSeverity") or "")
                if isinstance(score, (int, float)):
                    return float(score), severity
    return None, ""


def _keyword_variants(text: str) -> list[str]:
    """The keyword queries to try, most specific first.

    ``keywordSearch`` does not index a version, so ``tomcat 8.5.19`` matches
    nothing while ``tomcat`` matches 421 records from 2000-2006.  The version is
    therefore dropped before the core is shortened -- but dropping it is also
    why a keyword-only lookup can never answer "does THIS version have a CVE".

    That question is answered by :func:`_cpe_match_string` instead: an exact CPE
    for the observed version narrows ``tomcat 8.5.19`` from 421 mostly
    irrelevant records to the 66 that actually list 8.5.19 as affected.  Those
    two functions are complementary, not redundant, and the caller prefers the
    CPE axis whenever both a product and a version are known.

    Three queries is the ceiling: NVD allows five per thirty seconds, and the
    repository search needs one or two of them.
    """

    words = [word for word in re.split(r"[^A-Za-z0-9._-]+", text) if word]
    if not words:
        return []
    head = words[:8]
    variants = [" ".join(head)]
    core = [word for word in head if word.casefold() not in _STOPWORDS]
    without_version = [word for word in core if not re.fullmatch(r"\d+(?:\.\d+)+", word)]
    for candidate_words in (without_version[:4], without_version[:2]):
        candidate = " ".join(candidate_words)
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants[:3]


_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")

#: Words that describe the *category* rather than the product, so they must not
#: become the CPE product token.  ``apache tomcat 8.5.19`` has to resolve to
#: ``tomcat``; ``oracle weblogic server 12.2.1.3`` to ``weblogic_server``.
_PRODUCT_NOISE = frozenset(
    {
        "apache", "oracle", "microsoft", "the", "a", "an", "server", "http",
        "https", "version", "v", "release", "build", "and", "on", "in",
    }
)


#: Technique phrases that also carry a dotted version.  ``linux privilege
#: escalation 4.19`` would otherwise yield the product token
#: ``privilege_escalation`` and query a CPE that does not exist, hiding the real
#: answer behind an empty result.  Version pinning is only meaningful for a
#: *product*, so any of these words disqualifies the candidate.
_TECHNIQUE_WORDS = frozenset(
    {
        "escalation", "escalate", "injection", "traversal", "bypass", "shell",
        "exploit", "deserialization", "ssrf", "lfi", "rfi", "xxe", "ssti",
        "overflow", "enumeration", "bruteforce", "attack", "rce", "sqli", "xss",
        "hardening", "misconfiguration", "enumeration",
    }
)


def _product_and_version(text: str) -> tuple[str, str] | None:
    """Pull ``(cpe_product, version)`` out of a free-text keyword, if present.

    Deliberately conservative: it returns ``None`` rather than guessing, because
    a wrong CPE product silently answers a *different* product's CVEs -- which
    is the failure this whole axis exists to avoid.  The version must look like
    a dotted number, and the product must be the token right before it, with no
    technique word among the candidates.
    """

    words = [word for word in re.split(r"[^A-Za-z0-9._-]+", text) if word]
    for index, word in enumerate(words):
        if not _VERSION_RE.match(word):
            continue
        product_words: list[str] = []
        for candidate in reversed(words[:index]):
            lowered = candidate.casefold()
            if lowered in _STOPWORDS or lowered in _PRODUCT_NOISE:
                continue
            product_words.insert(0, lowered.strip("._-"))
            if len(product_words) >= 2:
                break
        product_words = [item for item in product_words if item]
        if not product_words:
            continue
        # A technique phrase is not a product: "privilege escalation 4.19"
        # describes an action, and no CPE is named after it.
        if any(item in _TECHNIQUE_WORDS for item in product_words):
            continue
        product = "_".join(product_words[-2:]) if len(product_words) > 1 else product_words[0]
        if not product or not re.fullmatch(r"[a-z0-9_.-]{2,60}", product):
            continue
        return product, word
    return None


def _cpe_match_string(product: str, version: str) -> str:
    """A CPE match string that pins one product at one version.

    The vendor is left as a wildcard on purpose.  An operator knows the banner
    ("Apache Tomcat/8.5.19"), not the CPE vendor token, and NVD's
    ``virtualMatchString`` accepts ``*`` for it -- verified against the live API:
    ``cpe:2.3:a:*:tomcat:8.5.19:*:*:*:*:*:*:*`` returns 66 records.
    """

    return f"cpe:2.3:a:*:{product}:{version}:*:*:*:*:*:*:*"


def _suggest_keywords(text: str) -> list[str]:
    """Sharper queries to hand back when the model's own keyword matched nothing.

    ``keywordSearch`` is an AND over tokens, so "apache tomcat 8.5.19 put jsp
    arbitrary file write" matches nothing at all while "tomcat 8.5.19" matches
    the version.  Handing the model the narrower form is far cheaper than letting
    it burn another turn guessing.
    """

    words = [word for word in re.split(r"[^A-Za-z0-9._-]+", text) if word]
    core = [word for word in words if word.casefold() not in _STOPWORDS]
    if not core:
        return []
    suggestions: list[str] = []
    version_at = next(
        (index for index, word in enumerate(core) if re.fullmatch(r"\d+(?:\.\d+)+", word)),
        None,
    )
    if version_at:
        product = core[version_at - 1] if version_at else core[0]
        suggestions.append(f"{product} {core[version_at]}")
    for size in (2, 3):
        if len(core) >= size:
            suggestions.append(" ".join(core[:size]))
    return list(dict.fromkeys(suggestions))[:3]


def _rank_cves(cves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Highest CVSS first, newest as the tie-break.

    A model reading three records wants the one that is worth testing, not the
    one NVD happened to list first.
    """

    return sorted(
        cves,
        key=lambda item: (
            float(item.get("cvss") or 0.0),
            str(item.get("published") or ""),
        ),
        reverse=True,
    )


def _extract_cves(payload: dict[str, Any]) -> list[dict[str, Any]]:
    vulnerabilities = payload.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        return []
    results: list[dict[str, Any]] = []
    for entry in vulnerabilities:
        cve = entry.get("cve") if isinstance(entry, dict) else None
        if not isinstance(cve, dict):
            continue
        cve_id = str(cve.get("id") or "").strip()
        if not cve_id:
            continue
        descriptions = cve.get("descriptions")
        summary = ""
        if isinstance(descriptions, list):
            for description in descriptions:
                if isinstance(description, dict) and description.get("lang") == "en":
                    summary = str(description.get("value") or "")
                    break
        score, severity = _severity(cve.get("metrics") or {})
        references: list[str] = []
        for ref in (cve.get("references") or [])[: _MAX_REFS * 2]:
            url = str((ref or {}).get("url") or "").strip() if isinstance(ref, dict) else ""
            if url.startswith("http") and len(references) < _MAX_REFS:
                references.append(url[:200])
        results.append(
            {
                "cve_id": cve_id,
                "summary": " ".join(summary.split())[:_MAX_SUMMARY],
                "cvss": score,
                "severity": severity,
                "published": str(cve.get("published") or "")[:10] or None,
                "references": references,
            }
        )
    return results


def _extract_repos(payload: dict[str, Any], *, cve_id: str) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    repos: list[dict[str, Any]] = []
    for item in items[: _MAX_REPOS]:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or "").strip()
        if not full_name:
            continue
        repos.append(
            {
                "repo": full_name[:120],
                "stars": int(item.get("stargazers_count") or 0),
                "description": " ".join(str(item.get("description") or "").split())[:220],
                "url": str(item.get("html_url") or "")[:200],
                "for_cve": cve_id,
            }
        )
    return repos


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Issue a request only to a declared host.

    ``ALLOWED_HOSTS`` has to be enforced, not merely documented: the whole point
    of a fixed host list is that no code path -- present or future -- can turn
    this tool into a general-purpose fetcher for the model.

    NVD calls are serialised and spaced out.  GitHub has no such gate: it allows
    sixty requests an hour and the caller already tolerates its 403.
    """

    host = (urlsplit(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise RuntimeError("HOST_NOT_ALLOWED")
    if host != "services.nvd.nist.gov":
        return await client.get(url, **kwargs)
    now = time.monotonic()
    slot = max(now, _nvd_next_slot[0])
    _nvd_next_slot[0] = slot + _NVD_MIN_INTERVAL
    if slot > now:
        await asyncio.sleep(slot - now)
    return await client.get(url, **kwargs)


async def _fetch_nvd_by_cpe(
    client: httpx.AsyncClient, *, product: str, version: str, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """CVEs whose CPE configuration actually lists this product AT this version.

    This is the only query that answers "is *my* version affected".  A keyword
    search for the product answers "does this product have CVEs", which for a
    long-lived product is always yes and tells a Worker nothing it can act on.
    """

    match = _cpe_match_string(product, version)
    response = await _get(
        client,
        NVD_ENDPOINT,
        params={
            "resultsPerPage": _NVD_PAGE_SIZE,
            "virtualMatchString": match,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"NVD_HTTP_{response.status_code}")
    payload = response.json()
    total = int(payload.get("totalResults") or 0)
    return _rank_cves(_extract_cves(payload))[:limit], total


async def _fetch_nvd(
    client: httpx.AsyncClient, *, keyword: str, cve_id: str, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Return the best ``limit`` records plus how many the query matched.

    Two behaviours here are not obvious from the API docs:

    * NVD orders by publication date **ascending** and caps a page at 2000.  A
      broad keyword therefore answers with the oldest records in the corpus --
      ``tomcat`` alone yields 421, the first fifty of which are from 2000-2006.
      When the match count exceeds one page, the *last* page is fetched instead,
      because a recent CVE is what a live target is more likely running.
    * Fetching all 421 takes thirty seconds and 3.9 MB and still ranks a Cisco
      advisory first, so the page is ranked locally and cut, never widened.
    """

    page = {"resultsPerPage": _NVD_PAGE_SIZE}

    if cve_id:
        response = await _get(client, NVD_ENDPOINT, params={**page, "cveId": cve_id})
        if response.status_code != 200:
            raise RuntimeError(f"NVD_HTTP_{response.status_code}")
        payload = response.json()
        total = int(payload.get("totalResults") or 0)
        return _rank_cves(_extract_cves(payload))[:limit], total

    total = 0
    for variant in _keyword_variants(keyword) or [keyword]:
        response = await _get(client, NVD_ENDPOINT, params={**page, "keywordSearch": variant})
        if response.status_code != 200:
            raise RuntimeError(f"NVD_HTTP_{response.status_code}")
        payload = response.json()
        total = int(payload.get("totalResults") or total)
        cves = _extract_cves(payload)
        if not cves:
            continue
        if total > _NVD_PAGE_SIZE:
            try:
                tail = await _get(
                    client,
                    NVD_ENDPOINT,
                    params={
                        "resultsPerPage": _NVD_PAGE_SIZE,
                        "startIndex": max(0, total - _NVD_PAGE_SIZE),
                        "keywordSearch": variant,
                    },
                )
                if tail.status_code == 200:
                    cves = _extract_cves(tail.json()) or cves
            except Exception:  # noqa: BLE001 - the first page is already usable
                pass
        return _rank_cves(cves)[:limit], total
    return [], total


async def _fetch_repos(client: httpx.AsyncClient, *, cve_id: str) -> list[dict[str, Any]]:
    response = await _get(
        client,
        GITHUB_SEARCH,
        params={"q": f"{cve_id} in:name,description", "sort": "stars", "order": "desc", "per_page": _MAX_REPOS},
        headers={"Accept": "application/vnd.github+json"},
    )
    if response.status_code != 200:
        # Rate limiting is the expected failure here (60 req/h unauthenticated).
        # Degrading to "no repos" still leaves the NVD record, which is the part
        # that names the vulnerability class.
        return []
    return _extract_repos(response.json(), cve_id=cve_id)


async def _fetch_repos_by_keyword(
    client: httpx.AsyncClient, *, keyword: str
) -> list[dict[str, Any]]:
    """Find harness/tooling repositories for a technique with no CVE attached.

    This is the privilege-escalation path: ``www-data`` on a Debian container is
    not a CVE, and the thing a Worker needs is the *harness* -- an exploit
    suggester, a SUID enumerator -- not an NVD record.  Without this, a lookup
    that resolves no CVE returns "nothing found" even though the technique is
    extremely well documented, and the Worker concludes the avenue is closed.
    """

    query = " ".join(keyword.split())[:120]
    if not query:
        return []
    response = await _get(
        client,
        GITHUB_SEARCH,
        params={
            "q": f"{query} exploit in:name,description",
            "sort": "stars",
            "order": "desc",
            "per_page": _MAX_REPOS,
        },
        headers={"Accept": "application/vnd.github+json"},
    )
    if response.status_code != 200:
        return []
    return _extract_repos(response.json(), cve_id="")


async def lookup_public_poc(
    *,
    keyword: str = "",
    cve_id: str = "",
    limit: int = 4,
    timeout: float = 9.0,
) -> dict[str, Any]:
    """Search NVD (and GitHub for PoC repos) for one product or CVE.

    Returns a bounded, structured summary.  Every failure is reported as
    ``{"success": False, "error": ...}`` rather than raised: a lookup that cannot
    reach the internet must not consume a Worker turn.
    """

    if not lookup_enabled():
        return {"success": False, "error": "LOOKUP_DISABLED"}
    text = " ".join(str(keyword or "").split())[:160]
    target_cve = str(cve_id or "").strip().upper()
    match = _CVE_RE.search(text) or _CVE_RE.search(target_cve)
    if match:
        target_cve = match.group(0).upper()
    if not text and not target_cve:
        return {"success": False, "error": "KEYWORD_REQUIRED"}

    key = (text.casefold(), target_cve)
    cached = _CACHE.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    request_timeout = max(2.0, min(float(timeout), 15.0))
    async with httpx.AsyncClient(
        timeout=request_timeout, follow_redirects=False, trust_env=True
    ) as client:
        try:
            # Prefer the version-exact axis.  A CVE that lists this exact
            # version is a fact about the target; a CVE that merely shares the
            # product name is a guess.  The keyword query is kept as the
            # fallback because the CPE token can be wrong (an operator writes
            # "Apache Tomcat/8.5.19" but NVD may call the product something
            # else), and an empty answer would be worse than a broad one.
            matched_on: dict[str, Any] = {}
            cves: list[dict[str, Any]] = []
            total = 0
            product_version = None if target_cve else _product_and_version(text)
            if product_version:
                product, version = product_version
                try:
                    cves, total = await asyncio.wait_for(
                        _fetch_nvd_by_cpe(
                            client, product=product, version=version, limit=limit
                        ),
                        timeout=request_timeout,
                    )
                    if cves:
                        matched_on = {
                            "axis": "cpe_version",
                            "product": product,
                            "version": version,
                            "cpe": _cpe_match_string(product, version),
                        }
                except Exception:  # noqa: BLE001 - fall through to the keyword axis
                    cves, total = [], 0
            if not cves:
                cves, total = await asyncio.wait_for(
                    _fetch_nvd(client, keyword=text, cve_id=target_cve, limit=limit),
                    timeout=request_timeout,
                )
                if cves and not target_cve:
                    # Say plainly that these only share the product name.  The
                    # model must not treat a product-wide match as "my version
                    # is affected".
                    matched_on = {
                        "axis": "keyword_product_only",
                        "product": (product_version or ("", ""))[0] or None,
                        "version": None,
                    }
                elif cves and target_cve:
                    matched_on = {"axis": "cve_id", "product": None, "version": None}
        except Exception as error:  # noqa: BLE001 - reported, never raised
            return {
                "success": False,
                "error": "NVD_UNAVAILABLE",
                "detail": type(error).__name__,
                "keyword": text or target_cve,
            }

        repos: list[dict[str, Any]] = []
        for record in cves[:2]:
            try:
                repos.extend(
                    await asyncio.wait_for(
                        _fetch_repos(client, cve_id=str(record["cve_id"])),
                        timeout=request_timeout,
                    )
                )
            except Exception:  # noqa: BLE001 - repo search is best effort
                continue
        if not repos and text:
            # No CVE resolved.  For a post-exploitation question ("linux
            # privilege escalation 4.19") the useful answer is the harness, not
            # an NVD record, so search repositories by the technique itself.
            try:
                repos.extend(
                    await asyncio.wait_for(
                        _fetch_repos_by_keyword(client, keyword=text),
                        timeout=request_timeout,
                    )
                )
            except Exception:  # noqa: BLE001 - still returns the NVD answer
                pass

    result: dict[str, Any] = {
        "success": True,
        "keyword": text or target_cve,
        "cve_id": target_cve or None,
        "cves": cves,
        "poc_repositories": repos,
        "found": bool(cves or repos),
        "total_results": total,
        #: Which axis produced this answer.  ``cpe_version`` means NVD lists the
        #: observed version as affected; ``keyword_product_only`` means the only
        #: shared fact is the product name, so every record is unconfirmed for
        #: this target.  A model that cannot see the difference wastes a round
        #: on a CVE for another branch of the product.
        "matched_on": matched_on,
        "usage": (
            "Reference material only. A CVE for this product is a hypothesis, not "
            "proof this target is vulnerable: confirm the exact version or behaviour "
            "against the target before acting, and never paste a PoC payload you "
            "have not adapted to this application."
        ),
    }
    if matched_on.get("axis") == "keyword_product_only" and cves:
        result["usage"] = (
            "matched_on.axis is keyword_product_only: these CVEs share only the "
            "product name, and NVD does not record this target's version among "
            "their affected CPEs. Re-read the target's version banner and retry "
            "with 'product major.minor.patch' to get a version-exact answer "
            "before spending a round on any of them."
        )
    if total > _NVD_PAGE_SIZE * 4:
        # Without this the model treats four records as the whole answer and
        # abandons a class that is well documented.
        result["hint"] = (
            f"{total} CVEs matched this keyword, so these are the highest-scoring of "
            f"the most recent {_NVD_PAGE_SIZE}. Add the product version or the "
            "vulnerable parameter, or pass the CVE id directly."
        )
    elif not cves and not repos:
        suggestions = _suggest_keywords(text)
        if suggestions:
            # An empty result is the model's keyword being too narrow for an AND
            # search, not the product having no CVE.  Say so, and say what to type.
            result["suggested_keywords"] = suggestions
            result["hint"] = (
                "Nothing matched that phrase: NVD requires every word to appear. "
                f"Retry with one of {suggestions}, or pass the CVE id."
            )
    elif not cves and repos:
        # Technique-level search (post-exploitation): repositories are the whole
        # answer here, so say that explicitly instead of implying a miss.
        result["hint"] = (
            "No CVE matched, but these repositories implement the technique. "
            "Read one before writing the harness yourself; adapt it to this "
            "target rather than pasting it."
        )
    _CACHE.put(key, result)
    return result
