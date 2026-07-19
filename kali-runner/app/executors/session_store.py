from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import secrets

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
        self._secrets: dict[tuple[str, str], str] = {}

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
        for key in [key for key in self._secrets if key[0] == run_id]:
            self._secrets.pop(key, None)

    def put_secret(self, run_id: str, value: str, purpose: str = "opaque") -> str:
        return_ref = f"sec_{secrets.token_urlsafe(18)}"
        self._secrets[(run_id, return_ref)] = value
        return return_ref

    def get_secret(self, run_id: str, ref: str) -> str:
        value = self._secrets.get((run_id, ref))
        if value is None:
            raise KeyError("secret reference not found")
        return value

    def list_secret_refs(self, run_id: str) -> list[dict]:
        return [{"secret_ref": ref, "value_present": bool(value)} for (owner, ref), value in self._secrets.items() if owner == run_id]

    def cookie_secret_ref(self, run_id: str, session_name: str, cookie_name: str) -> str | None:
        item = self._get(run_id, session_name, create=False)
        value = item.cookies.get(cookie_name) if item else None
        return self.put_secret(run_id, value, "cookie") if value else None

    def set_cookie_ref(self, run_id: str, session_name: str, cookie_name: str, ref: str) -> None:
        self.cookies(run_id, session_name).set(cookie_name, self.get_secret(run_id, ref))


session_store = SessionStore()
