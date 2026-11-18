"""The arena routing regression that sent every box to the SQL oracle.

Root cause: ``classify_challenge`` treated ``metadata["adapter"]`` as proof of a
SQL challenge at confidence 100.  The arena stamps ``adapter: "dasctf_arena"``
on **every** imported challenge, so a Spring SpEL box and a phpMyAdmin LFI box
were both classified SQLI and dispatched ``sql_boolean_compare`` -- a tool the
target had no surface for.  The round then spent its budget on a SQL oracle and
returned unsolved while the real exploit was never attempted.

These tests pin the routing contract, using the real metadata shapes observed in
the 2026-10-21 arena batch.
"""

from __future__ import annotations

import pytest

from app.solver.muteki.recon.fingerprint import classify_challenge

# Exactly the metadata the arena writes for an imported challenge: an adapter
# name, a category, a difficulty and no vulnerability hint whatsoever.
ARENA_METADATA = {
    "points": 1,
    "adapter": "dasctf_arena",
    "category": "Real",
    "difficulty": "Easy",
    "friendly_id": "PCHAL-2026-5467",
    "has_container": True,
}


def test_platform_adapter_is_not_a_sql_signal() -> None:
    """``adapter`` names the import path, not the vulnerability."""

    assert classify_challenge(dict(ARENA_METADATA)) is None


def test_bare_dbms_is_not_enough() -> None:
    """A bundled MySQL is not an injectable parameter."""

    assert classify_challenge({**ARENA_METADATA, "dbms": "mysql"}) is None
    declared = classify_challenge(
        {**ARENA_METADATA, "dbms": "mysql", "description": "SQL injection in the id parameter"}
    )
    assert declared is not None
    assert declared.classification == "SQLI"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("[PHPMYADMIN]CVE-2018-12613", "PATH_TRAVERSAL"),
        ("[Spring]CVE-2016-4977", "SSTI"),
        ("[Rails]CVE-2018-3760", "PATH_TRAVERSAL"),
        ("[WebLogic]CVE-2018-2894", "FILE_UPLOAD"),
        ("[WebLogic]CVE-2018-2628", "COMMAND_INJECTION"),
        ("[Tomcat]CVE-2017-12615", "FILE_UPLOAD"),
    ],
)
def test_cve_title_decides_the_family(name: str, expected: str) -> None:
    result = classify_challenge({**ARENA_METADATA, "name": name, "friendly_id": "PCHAL-1"})
    assert result is not None
    assert result.classification == expected
    assert result.confidence >= 90


def test_cve_separator_variants_all_match() -> None:
    for variant in ("CVE-2018-12613", "cve_2018_12613", "CVE 2018 12613"):
        result = classify_challenge({"description": f"exploit {variant} on this box"})
        assert result is not None, variant
        assert result.classification == "PATH_TRAVERSAL", variant


def test_explicit_vulnerability_type_still_wins() -> None:
    """An operator's explicit declaration outranks the CVE table."""

    result = classify_challenge(
        {"vulnerability_type": "SQLI", "name": "[Spring]CVE-2016-4977"}
    )
    assert result is not None
    assert result.classification == "SQLI"
    assert result.confidence == 100


def test_arena_batch_is_not_dispatched_to_the_sql_oracle() -> None:
    """The end-to-end property that matters: no SQLI for these five boxes."""

    names = [
        "[PHPMYADMIN]CVE-2018-12613",
        "[Spring]CVE-2016-4977",
        "[Rails]CVE-2018-3760",
        "[WebLogic]CVE-2018-2894",
        "[WebLogic]CVE-2018-2628",
    ]
    classified = [
        classify_challenge({**ARENA_METADATA, "name": name}) for name in names
    ]
    assert all(item is not None for item in classified)
    assert not any(item.classification == "SQLI" for item in classified)
