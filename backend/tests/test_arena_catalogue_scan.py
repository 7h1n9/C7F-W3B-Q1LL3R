"""The catalogue paginates, and the enqueue path has to actually use that.

Before this, both enqueue endpoints read a single page of 100.  Two consequences,
and the second is the dangerous one:

* ``enqueue`` filtered ``category``/``difficulty`` **locally**, so asking for a
  category whose challenges sit past page 1 returned "no match" -- a wrong answer
  wearing the costume of a valid one.
* ``enqueue-ids`` looked each named id up in that one page, so every challenge
  past page 1 came back ``not_in_catalogue``.  BUUCTF holds 3244 challenges;
  naming any of the 3144 past page 1 blamed the caller for our own horizon.

The platform does the filtering and the paging (``page``, ``difficulty``,
``unsolved_only`` are all real tool parameters, confirmed against the live
``inputSchema``), so these tests pin the call contract AND the scan's stopping
rules -- an unbounded crawl of a 3244-item catalogue is its own bug.
"""

from __future__ import annotations

from app.api.v1.arena import ARENA_CATALOGUE_MAX_PAGES, ARENA_CATALOGUE_PAGE_SIZE, _catalogue_scan
from app.services.arena_mcp import ArenaMcpClient


class FakeCatalogue:
    """A catalogue that pages and filters the way the platform does."""

    def __init__(self, rows: list[dict], *, ignore_filters: bool = False) -> None:
        self.rows = rows
        self.ignore_filters = ignore_filters
        self.calls: list[dict] = []

    async def list_practice_challenges(
        self,
        *,
        practice_ground_id: str,
        category: str | None = None,
        difficulty: str | None = None,
        limit: int = 50,
        page: int | None = None,
        unsolved_only: bool = False,
    ) -> dict:
        self.calls.append(
            {
                "page": page,
                "category": category,
                "difficulty": difficulty,
                "unsolved_only": unsolved_only,
                "limit": limit,
            }
        )
        rows = list(self.rows)
        if not self.ignore_filters:
            if category:
                rows = [row for row in rows if row["category"] == category]
            if difficulty:
                rows = [row for row in rows if row["difficulty"] == difficulty]
            if unsolved_only:
                rows = [row for row in rows if not row["is_solved"]]
        total = len(rows)
        start = ((page or 1) - 1) * limit
        total_page = (total + limit - 1) // limit if total else 0
        return {
            "items": rows[start : start + limit],
            "page": page or 1,
            "page_size": limit,
            "total": total,
            "total_page": total_page,
        }


def _row(index: int, *, category: str, container: bool, solved: bool = False) -> dict:
    return {
        "id": f"id-{index:04d}",
        "name": f"challenge {index}",
        "category": category,
        "difficulty": "Easy",
        "has_container": container,
        "is_solved": solved,
    }


async def test_category_only_present_past_page_one_is_found() -> None:
    """The bug this fixes: a server-side filter must reach beyond page 1."""
    rows = [_row(i, category="A", container=True) for i in range(150)]
    rows += [_row(1000 + i, category="B", container=True) for i in range(20)]
    catalogue = FakeCatalogue(rows)

    matched, total, truncated = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=5, category="B", order="as_listed"
    )

    assert total == 20
    assert truncated is False
    assert [row["category"] for row in matched] == ["B"] * 20
    # The filter went to the platform rather than being applied to page 1 here.
    assert all(call["category"] == "B" for call in catalogue.calls)


async def test_unsolved_only_is_filtered_server_side() -> None:
    rows = [_row(i, category="A", container=True, solved=i % 2 == 0) for i in range(10)]
    catalogue = FakeCatalogue(rows)

    matched, total, _ = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=10, unsolved_only=True
    )

    assert catalogue.calls[0]["unsolved_only"] is True
    assert total == 5
    assert all(not row["is_solved"] for row in matched)


async def test_as_listed_stops_at_the_first_sufficient_page() -> None:
    """A 3244-item catalogue must not be crawled to fill a 5-item request."""
    catalogue = FakeCatalogue([_row(i, category="A", container=True) for i in range(3244)])

    matched, _, _ = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=5, order="as_listed"
    )

    assert len(catalogue.calls) == 1
    assert len(matched) == ARENA_CATALOGUE_PAGE_SIZE


async def test_container_first_stops_once_enough_containers_are_seen() -> None:
    rows = [_row(i, category="A", container=False) for i in range(100)]
    rows += [_row(200 + i, category="A", container=True) for i in range(100)]
    catalogue = FakeCatalogue(rows)

    matched, _, _ = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=3, order="container_first"
    )

    assert len(catalogue.calls) == 2
    containers = [row for row in matched if row["has_container"]]
    assert len(containers) == 100


async def test_scan_reports_truncation_instead_of_implying_it_saw_everything() -> None:
    """5000 rows and a request for all of them: the page cap bites at 4000 and
    the caller is told, rather than getting 4000 rows that look like the lot."""
    rows = [_row(i, category="A", container=False) for i in range(5000)]
    catalogue = FakeCatalogue(rows)

    matched, total, truncated = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=5000, order="as_listed"
    )

    assert truncated is True
    assert total == 5000
    assert len(catalogue.calls) == ARENA_CATALOGUE_MAX_PAGES
    assert len(matched) == ARENA_CATALOGUE_MAX_PAGES * ARENA_CATALOGUE_PAGE_SIZE


async def test_a_request_fully_satisfied_at_the_cap_is_not_called_truncated() -> None:
    """Reaching the cap is only truncation when it cost something: with all 4000
    rows in hand there is nothing left to fetch, so the flag stays down."""
    rows = [_row(i, category="A", container=False) for i in range(4000)]
    catalogue = FakeCatalogue(rows)

    matched, _, truncated = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=4000, order="as_listed"
    )

    assert truncated is False
    assert len(matched) == 4000


async def test_local_filter_still_guards_against_a_platform_that_ignores_it() -> None:
    catalogue = FakeCatalogue(
        [_row(i, category="A", container=True) for i in range(10)], ignore_filters=True
    )

    matched, _, _ = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=5, category="B", order="as_listed"
    )

    assert matched == []


async def test_a_page_emptied_by_the_local_filter_does_not_end_the_scan() -> None:
    """End-of-catalogue is judged on the raw page, not the filtered one.

    If the platform ever stopped honouring ``category``, the local guard would
    empty whole pages.  Judging "the catalogue ended" on the filtered length
    would then stop at page 1 and report no matches -- the same silent wrong
    answer the server-side filter exists to prevent.
    """
    rows = [_row(i, category="A", container=True) for i in range(300)]
    rows += [_row(9000 + i, category="B", container=True) for i in range(3)]
    catalogue = FakeCatalogue(rows, ignore_filters=True)

    matched, _, truncated = await _catalogue_scan(
        catalogue, practice_ground_id="g", wanted=3, category="B", order="as_listed"
    )

    assert len(catalogue.calls) == 4  # B only appears on the last page
    assert truncated is False
    assert [row["category"] for row in matched] == ["B"] * 3


async def test_wrapper_sends_pagination_and_filter_arguments() -> None:
    client = ArenaMcpClient(connection_id="c", base_url="https://arena/api/open/v1/user", token="t")
    seen: dict = {}

    async def fake_call(name: str, arguments: dict | None = None):
        seen["name"] = name
        seen["arguments"] = arguments
        return {"items": []}

    client.call = fake_call  # type: ignore[method-assign]

    await client.list_practice_challenges(
        practice_ground_id="g",
        category="WEB",
        difficulty="Hard",
        limit=100,
        page=3,
        unsolved_only=True,
    )

    assert seen["name"] == "ctf2_list_practice_challenges"
    assert seen["arguments"] == {
        "practice_ground_id": "g",
        "limit": 100,
        "category": "WEB",
        "difficulty": "Hard",
        "page": 3,
        "unsolved_only": True,
    }


async def test_wrapper_omits_absent_options() -> None:
    """No page 1 leak: page is only sent when the caller asked for a page."""
    client = ArenaMcpClient(connection_id="c", base_url="https://arena/api/open/v1/user", token="t")
    seen: dict = {}

    async def fake_call(name: str, arguments: dict | None = None):
        seen["arguments"] = arguments
        return {"items": []}

    client.call = fake_call  # type: ignore[method-assign]

    await client.list_practice_challenges(practice_ground_id="g", limit=50)

    assert seen["arguments"] == {"practice_ground_id": "g", "limit": 50}
