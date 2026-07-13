from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx


@dataclass
class SessionEntry:
    cookies: httpx.Cookies = field(default_factory=httpx.Cookies)
    last_used: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionStore:
    """Runner-local, run-isolated cookie jar. Cookie values never leave this class."""

    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl = timedelta(seconds=max(60, ttl_seconds))
        self._items: dict[tuple[str, str], SessionEntry] = {}

    def _get(self, run_id: str, session_name: str, create: bool = True) -> SessionEntry | None:
        key = (run_id, session_name)
        item = self._items.get(key)
        if item and datetime.now(UTC) - item.last_used > self.ttl:
            self._items.pop(key, None)
            item = None
        if item is None and create:
            item = SessionEntry()
            self._items[key] = item
        if item:
            item.last_used = datetime.now(UTC)
        return item

    def cookies(self, run_id: str, session_name: str) -> httpx.Cookies:
        item = self._get(run_id, session_name)
        assert item is not None
        return item.cookies

    def update(self, run_id: str, session_name: str, response: httpx.Response) -> None:
        item = self._get(run_id, session_name)
        assert item is not None
        item.cookies.update(response.cookies)

    def inspect(self, run_id: str, session_name: str) -> dict:
        item = self._get(run_id, session_name, create=False)
        return {"exists": bool(item), "cookie_names": sorted(item.cookies.keys()) if item else [], "authenticated": bool(item and len(item.cookies) > 0)}

    def clear(self, run_id: str, session_name: str) -> None:
        self._items.pop((run_id, session_name), None)

    def clear_run(self, run_id: str) -> None:
        for key in [key for key in self._items if key[0] == run_id]:
            self._items.pop(key, None)


session_store = SessionStore()
