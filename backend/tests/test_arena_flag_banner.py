"""The arena's flag banner is the PLATFORM's, not the mirrored challenge's.

An audit of the live database found 15 recovered flags that do not fit the
challenge row's declared pattern -- 13 of them on Runs marked
``COMPLETED_SOLVED`` -- even though the platform had **accepted** them:

    challenge declares ``flag\\{[^}]+\\}``   value is ``CTF2{8e7aece6-...}``   accepted

The rows were seeded with the generic ``flag{...}`` placeholder by an older
import and the mirror only ever stamped a banner onto a row that had *none*, so
the wrong pattern was permanent.  That was survivable only while nothing
re-checked the declared pattern; once flag verification started re-applying it,
the stale placeholder would have rejected all 13 -- marking genuine solves
"unsolved" and blocking their submission.

These tests pin the two halves: the platform setting is the authority, and an
explicit per-challenge override is still honoured.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.challenge import normalize_flag_pattern
from app.services import arena_solve

PLACEHOLDER = r"flag\{[^}]+\}"
CTF2 = r"CTF2\{[^}]+\}"
DASCTF = r"DASCTF\{[^}]+\}"


class _Session:
    """The slice of ``AsyncSession`` the mirror touches."""

    def __init__(self, existing: object | None) -> None:
        self._existing = existing
        self.added: list[object] = []
        self.commits = 0

    async def scalar(self, _statement: object) -> object | None:
        return self._existing

    def add(self, item: object) -> None:
        self.added.append(item)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _item: object) -> None:
        return None


def _detail(**overrides: object) -> dict:
    detail = {"id": "ch-1", "name": "BUU XXE COURSE 1", "description": "x"}
    detail.update(overrides)
    return detail


@pytest.fixture(autouse=True)
def _platform_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        arena_solve,
        "get_settings",
        lambda: SimpleNamespace(arena_flag_banner="CTF2"),
    )


async def _upsert(existing: object | None, detail: dict | None = None):
    session = _Session(existing)
    item = await arena_solve.upsert_local_challenge(
        session,
        connection=SimpleNamespace(id="conn-1"),
        practice_ground_id="pg-1",
        detail=detail or _detail(),
    )
    return item, session


async def test_a_stale_placeholder_is_refreshed_from_the_platform() -> None:
    """The exact case that would otherwise reject 13 accepted flags."""

    existing = SimpleNamespace(flag_pattern=PLACEHOLDER, metadata_json={})

    item, _session = await _upsert(existing)

    assert item.flag_pattern == CTF2


async def test_a_stale_banner_from_a_previous_setting_is_refreshed() -> None:
    existing = SimpleNamespace(flag_pattern=DASCTF, metadata_json={})

    item, _session = await _upsert(existing)

    assert item.flag_pattern == CTF2


async def test_a_correct_platform_pattern_is_kept() -> None:
    existing = SimpleNamespace(flag_pattern=CTF2, metadata_json={})

    item, _session = await _upsert(existing)

    assert item.flag_pattern == CTF2


async def test_a_new_challenge_gets_the_platform_pattern() -> None:
    item, session = await _upsert(None)

    assert item.flag_pattern == CTF2
    assert session.added, "a new mirror must be inserted"


async def test_an_explicit_override_is_authoritative() -> None:
    """A genuinely per-challenge shape must still be expressible."""

    override = r"MYCTF\{[a-z]+\}"
    existing = SimpleNamespace(
        flag_pattern=PLACEHOLDER,
        metadata_json={"flag_pattern_override": override},
    )

    item, _session = await _upsert(existing)

    assert item.flag_pattern == override


async def test_the_banner_setting_is_the_single_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pointing the setting at another platform must be enough to switch."""

    monkeypatch.setattr(
        arena_solve,
        "get_settings",
        lambda: SimpleNamespace(arena_flag_banner="DASCTF"),
    )
    existing = SimpleNamespace(flag_pattern=PLACEHOLDER, metadata_json={})

    item, _session = await _upsert(existing)

    assert item.flag_pattern == DASCTF


async def test_a_blank_setting_falls_back_to_the_default_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        arena_solve,
        "get_settings",
        lambda: SimpleNamespace(arena_flag_banner=""),
    )
    existing = SimpleNamespace(flag_pattern=PLACEHOLDER, metadata_json={})

    item, _session = await _upsert(existing)

    assert item.flag_pattern == normalize_flag_pattern(
        arena_solve.DEFAULT_ARENA_FLAG_BANNER
    )
    assert arena_solve.DEFAULT_ARENA_FLAG_BANNER == "CTF2"
