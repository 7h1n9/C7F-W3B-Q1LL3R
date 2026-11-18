"""Every solved challenge must get its OWN template.

The distilled-template store de-duplicates by **filename**, and the vendored
sanitiser replaces every run of characters outside ``[a-zA-Z0-9_-]`` with a
single underscore.  A Chinese challenge name therefore collapses to ``_``, so the
file becomes ``<category>___.yaml`` -- and 13 distinct solved challenges were
measured sharing exactly that one file.  Each new solve silently destroyed the
previous template, which is why only 16 positive templates existed for 27
distinct solved challenges.

The name itself must survive (it feeds the keywords and the recalled text), so the
fix only appends a stable ASCII discriminator derived from the challenge id.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.solver.muteki.runtime.muteki_runtime import _disambiguate_template_name

CHINESE = "每日流量邮报：数据院碎片还原"


def _challenge(challenge_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=challenge_id)


def test_two_chinese_names_no_longer_share_a_filename() -> None:
    first = _disambiguate_template_name(CHINESE, _challenge("0924e804-86ee-4e4d-9738-abcd"))
    second = _disambiguate_template_name(
        "Sentinel-1B:???? FLAG", _challenge("dbb4388a-1111-2222-3333-abcd")
    )

    assert first != second
    # The names must still carry their original characters.
    assert CHINESE in first
    assert "Sentinel-1B" in second


def test_the_discriminator_is_stable_for_the_same_challenge() -> None:
    """Re-solving a challenge must overwrite its own template, not create a second."""

    challenge = _challenge("0924e804-86ee-4e4d-9738-abcd")

    assert _disambiguate_template_name(CHINESE, challenge) == _disambiguate_template_name(
        CHINESE, challenge
    )


def test_the_resulting_filename_is_unique_under_the_store_sanitiser() -> None:
    """The whole point: the store's own rule must now produce distinct files."""

    import re

    def safe(name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", name) or "template"

    left = safe(_disambiguate_template_name(CHINESE, _challenge("0924e804-86ee-4e4d")))
    right = safe(_disambiguate_template_name(CHINESE, _challenge("aaaabbbb-cccc-dddd")))

    assert left != right


def test_an_ascii_name_is_left_untouched() -> None:
    """Established keys must not move: that would orphan the existing files."""

    assert _disambiguate_template_name("BUU BRUTE 1", _challenge("x")) == "BUU BRUTE 1"
    assert _disambiguate_template_name("EasySQL", _challenge("x")) == "EasySQL"
    assert (
        _disambiguate_template_name("[34C3CTF 2017]urlstorage", _challenge("x"))
        == "[34C3CTF 2017]urlstorage"
    )


def test_a_challenge_without_an_id_degrades_to_the_old_behaviour() -> None:
    """Better a shared file than a crash inside post-run distillation."""

    assert _disambiguate_template_name(CHINESE, SimpleNamespace()) == CHINESE
    assert _disambiguate_template_name("", _challenge("x")) == ""
