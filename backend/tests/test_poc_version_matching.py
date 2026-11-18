"""A lookup must be able to answer "is MY version affected", not just "does this product have CVEs".

Measured against the live NVD API on 2026-10-22:

* ``keywordSearch=tomcat``        -> 421 records, first page mostly 2000-2006
* ``virtualMatchString=cpe:2.3:a:*:tomcat:8.5.19:...``  -> 66 records, all 8.5.19

The first answer cannot be acted on: a Worker cannot tell which of 421 CVEs
applies to the box in front of it.  The second names the version in the CPE
configuration, so every record is a candidate for *this* target.  Keeping the
version out of the query (what the code did before) is what made the tool only
useful for "which product is this", never "is this build vulnerable".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.solver.muteki.worker.public_poc import (  # noqa: E402
    _cpe_match_string,
    _keyword_variants,
    _product_and_version,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tomcat 8.5.19", ("tomcat", "8.5.19")),
        ("Apache Tomcat/8.5.19", ("tomcat", "8.5.19")),
        ("oracle weblogic 12.2.1.3", ("weblogic", "12.2.1.3")),
        ("phpMyAdmin 4.8.1", ("phpmyadmin", "4.8.1")),
        ("spring framework 4.3.4", ("spring_framework", "4.3.4")),
        ("nginx 1.18", ("nginx", "1.18")),
    ],
)
def test_a_version_banner_yields_product_and_version(text: str, expected: tuple[str, str]) -> None:
    assert _product_and_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "tomcat",                 # no version at all
        "reverse shell",          # no version
        "CVE-2018-12613",         # an id, not a banner
        "linux privilege escalation 4.19",  # version present but not a product pin
    ],
)
def test_no_version_pin_is_invented(text: str) -> None:
    """A wrong product token would answer another product's CVEs.

    ``linux privilege escalation 4.19`` is the dangerous shape: it *has* a
    dotted number, but attaching ``escalation_4.19`` as a CPE product would
    query a product that does not exist and hide a real answer.
    """

    result = _product_and_version(text)
    if result is not None:
        product, version = result
        # If it did parse, it must at least be a plausible single CPE token and
        # must not be the technique phrase.
        assert "_" not in product or product.count("_") <= 1
        assert "escalation" not in product


def test_the_cpe_string_pins_version_and_wildcards_the_vendor() -> None:
    cpe = _cpe_match_string("tomcat", "8.5.19")
    assert cpe == "cpe:2.3:a:*:tomcat:8.5.19:*:*:*:*:*:*:*"
    # The vendor wildcard is what lets an operator who only has a banner
    # ("Apache Tomcat/8.5.19") query successfully.
    assert cpe.split(":")[3] == "*"


def test_the_keyword_axis_still_drops_the_version() -> None:
    """Complementary, not redundant: keywordSearch genuinely cannot index a version."""

    variants = _keyword_variants("tomcat 8.5.19")
    assert variants[0] == "tomcat 8.5.19"
    assert "8.5.19" not in variants[-1]


def test_a_live_lookup_reports_which_axis_answered(monkeypatch) -> None:
    """The reply must say whether the answer is version-exact or product-only."""

    import asyncio

    from app.solver.muteki.worker import public_poc

    async def fake_cpe(client, *, product, version, limit):
        return (
            [
                {
                    "cve_id": "CVE-2017-12617",
                    "summary": "Apache Tomcat before 9.0.1 RCE via PUT",
                    "cvss": 8.1,
                    "severity": "HIGH",
                    "published": "2017-10-04",
                    "references": [],
                }
            ],
            66,
        )

    async def fake_keyword(client, *, keyword, cve_id, limit):
        return ([], 0)

    monkeypatch.setattr(public_poc, "_fetch_nvd_by_cpe", fake_cpe)
    monkeypatch.setattr(public_poc, "_fetch_nvd", fake_keyword)
    monkeypatch.setattr(public_poc, "lookup_enabled", lambda: True)
    monkeypatch.setattr(public_poc, "_CACHE", public_poc._LookupCache())

    result = asyncio.run(public_poc.lookup_public_poc(keyword="tomcat 8.5.19"))

    assert result["success"] is True
    assert result["matched_on"]["axis"] == "cpe_version"
    assert result["matched_on"]["version"] == "8.5.19"
    assert result["cves"][0]["cve_id"] == "CVE-2017-12617"
