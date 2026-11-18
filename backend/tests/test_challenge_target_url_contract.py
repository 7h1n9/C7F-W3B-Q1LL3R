"""``target_url`` must be renderable on read even when the stored row is stale.

The failure this pins down
--------------------------
An arena ground can publish an environment's address as a bare ``host:port``
(no scheme).  ``ArenaSolve`` wrote that verbatim into ``challenges.target_url``.

``urlsplit("direct-ctf2.dasctf.com:29806")`` reads ``direct-ctf2.dasctf.com`` as
the **scheme** and reports ``hostname is None`` -- so the value was doubly
broken: the read schema rejected it, and the derived ``allowed_hosts`` came out
empty.  Because ``ChallengeRead`` inherits the *input* contract, one such row
failed ``ChallengeRead.model_validate`` and turned the whole ``GET /challenges``
list into HTTP 500.  ``GET /challenges`` is what the UI's 题库 page calls, so
the page rendered nothing at all.

The fix has three parts, all pinned here:

* the write path normalises the address before storing it;
* the read path tolerates a legacy value instead of rejecting it;
* ``allowed_hosts`` is derived from the normalised address, not an empty string.

The input contract itself must stay strict: an operator creating a challenge
still has to supply a real absolute URL.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.net import ensure_absolute_http_url
from app.schemas.challenge import ChallengeInput, ChallengeRead
from app.services.arena_lease import hosts_from_access_urls

# --------------------------------------------------------------------------- #
# the helper
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("direct-ctf2.dasctf.com:29806", "http://direct-ctf2.dasctf.com:29806"),
        ("example.com", "http://example.com"),
        ("http://example.com:80", "http://example.com:80"),
        # An explicit scheme must never be downgraded.
        ("https://example.com", "https://example.com"),
        ("  spaced.example:8080  ", "http://spaced.example:8080"),
    ],
)
def test_a_bare_address_gains_a_scheme(given: str, expected: str) -> None:
    assert ensure_absolute_http_url(given) == expected


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_address_stays_blank(blank: object) -> None:
    assert ensure_absolute_http_url(blank) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the read path must render, not reject
# --------------------------------------------------------------------------- #


def _read_row(**overrides: object) -> ChallengeRead:
    base: dict[str, object] = {
        "id": "c-1",
        "name": "BUU BURP COURSE 1",
        "challenge_type": "WEB_TARGET",
        "created_at": "2026-10-15T00:00:00",
        "updated_at": "2026-10-15T00:00:00",
    }
    base.update(overrides)
    return ChallengeRead.model_validate(base)


def test_a_legacy_bare_address_is_rendered_not_rejected() -> None:
    """This is the row that made GET /challenges return HTTP 500."""
    row = _read_row(target_url="direct-ctf2.dasctf.com:28890", allowed_hosts=[])
    assert row.target_url == "http://direct-ctf2.dasctf.com:28890"


def test_a_challenge_with_no_target_is_renderable() -> None:
    """``[无flag]`` teaching challenges legitimately carry no target."""
    row = _read_row(name="[5.1.4 无flag]", target_url=None, allowed_hosts=[])
    assert row.target_url is None


def test_an_empty_host_allowlist_is_renderable() -> None:
    """A stale row must not fail on the allowlist rule either."""
    row = _read_row(target_url="http://example.com", allowed_hosts=[])
    assert row.allowed_hosts == []


# --------------------------------------------------------------------------- #
# the input contract must NOT be weakened
# --------------------------------------------------------------------------- #


def test_creating_a_challenge_still_requires_an_absolute_url() -> None:
    with pytest.raises(ValidationError, match="absolute HTTP"):
        ChallengeInput(
            name="x",
            challenge_type="WEB_TARGET",
            target_url="direct-ctf2.dasctf.com:28890",
            allowed_hosts=["direct-ctf2.dasctf.com"],
        )


def test_creating_a_challenge_still_requires_the_host_to_be_allowed() -> None:
    with pytest.raises(ValidationError, match="allowed_hosts"):
        ChallengeInput(
            name="x",
            challenge_type="WEB_TARGET",
            target_url="http://example.com",
            allowed_hosts=[],
        )


# --------------------------------------------------------------------------- #
# the derived allowlist
# --------------------------------------------------------------------------- #


def test_a_bare_access_url_still_yields_its_hostname() -> None:
    """``urlsplit`` reads a bare ``host:port`` as a scheme, so the host was lost."""
    hosts = hosts_from_access_urls({"access_url": "direct-ctf2.dasctf.com:29806"})
    assert hosts == ["direct-ctf2.dasctf.com"]


def test_a_normal_access_url_still_yields_its_hostname() -> None:
    hosts = hosts_from_access_urls(
        {"access_url": "http://abc.http-ctf2.dasctf.com:80"}
    )
    assert hosts == ["abc.http-ctf2.dasctf.com"]


def test_access_urls_list_is_also_normalised() -> None:
    hosts = hosts_from_access_urls(
        {"access_urls": [{"url": "bare.example:9000"}, {"url": "http://other.example"}]}
    )
    assert hosts == ["bare.example", "other.example"]
