from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

ToolExecutor = Callable[[str, dict[str, Any], str, str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ReconObservation:
    endpoint: str
    status_code: int | None
    summary: str
    cookie_names: tuple[str, ...] = ()
    redirected_to_login: bool = False
    framework: str | None = None
    jwt_detected: bool = False
    links: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconReport:
    observations: tuple[ReconObservation, ...]
    endpoints: tuple[str, ...]
    auth_required: bool
    session_cookie_names: tuple[str, ...]
    frameworks: tuple[str, ...]
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)


class BreadthScanner:
    """Run bounded, same-session HTTP reconnaissance.

    The scanner deliberately requests common public/application routes even
    when they return 404. A 401/403/302 response is useful evidence for an
    authentication boundary, while the request budget prevents an unbounded
    crawler from replacing the Solver loop.
    """

    _COMMON_PATHS = ("/", "/dashboard", "/tickets", "/announcements", "/api/health", "/api/status")
    _FRAMEWORK_HEADERS = ("server", "x-powered-by")
    _FLAG_RE = re.compile(r"flag\{[^}\r\n]{1,200}\}", re.I)

    def __init__(self, execute_tool: ToolExecutor, *, max_requests: int = 12) -> None:
        self.execute_tool = execute_tool
        self.max_requests = max(4, int(max_requests))

    async def scan(self, *, base_url: str, workspace_id: str, run_id: str, session_name: str = "muteki-recon") -> ReconReport:
        observations: list[ReconObservation] = []
        seen: set[str] = set()
        evidence_refs: list[str] = []

        async def request(url: str) -> None:
            if len(observations) >= self.max_requests:
                return
            normalized = urljoin(base_url.rstrip("/") + "/", url)
            if normalized in seen:
                return
            seen.add(normalized)
            result = await self.execute_tool(
                "http_session_request",
                {"session_name": session_name, "method": "GET", "url": normalized, "follow_redirects": False},
                workspace_id,
                run_id,
            )
            output = dict(getattr(result, "output", {}) or {})
            status = _int(output.get("status_code") or _nested(output, "structured_result", "status_code"))
            body = str(output.get("body_excerpt") or output.get("body") or output.get("summary") or "")
            headers = output.get("headers") if isinstance(output.get("headers"), dict) else {}
            cookie_names = _cookie_names(output)
            location = str(headers.get("location") or headers.get("Location") or "")
            final_url = str(output.get("final_url") or "")
            redirected_to_login = any("login" in value.casefold() for value in (location, final_url))
            framework = _framework(headers)
            jwt_detected = _jwt_detected(body, cookie_names)
            links = tuple(_links(body, normalized))
            refs = tuple(str(item) for item in getattr(result, "evidence_refs", ()) or ())
            evidence_refs.extend(refs)
            observations.append(ReconObservation(normalized, status, _summary(body, status), cookie_names, redirected_to_login, framework, jwt_detected, links, refs))

        # Session creation is not counted as a target request.
        await self.execute_tool("http_session_request", {"operation": "create", "session_name": session_name}, workspace_id, run_id)
        for path in self._COMMON_PATHS:
            await request(path)
        discovered_links = []
        for item in observations:
            discovered_links.extend(item.links)
        for link in discovered_links:
            await request(link)

        endpoints = tuple(dict.fromkeys(item.endpoint for item in observations if item.status_code != 404 or item.endpoint.endswith("/")))
        cookies = tuple(dict.fromkeys(cookie for item in observations for cookie in item.cookie_names))
        frameworks = tuple(dict.fromkeys(item.framework for item in observations if item.framework))
        auth_required = any(
            item.status_code in {401, 403}
            or item.redirected_to_login
            or any(marker in item.summary.casefold() for marker in ("login", "sign in", "unauthorized"))
            for item in observations
        )
        return ReconReport(tuple(observations), endpoints, auth_required, cookies, frameworks, tuple(dict.fromkeys(evidence_refs)))


def _nested(value: dict[str, Any], key: str, child: str) -> Any:
    nested = value.get(key)
    return nested.get(child) if isinstance(nested, dict) else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _cookie_names(output: dict[str, Any]) -> tuple[str, ...]:
    values = output.get("cookie_names") or _nested(output, "extracted_facts", "cookie_names") or []
    if not values:
        headers = output.get("headers") if isinstance(output.get("headers"), dict) else {}
        values = headers.get("set-cookie") or headers.get("Set-Cookie") or []
    if isinstance(values, str):
        values = [values]
    return tuple(str(item).split("=", 1)[0].strip() for item in values if str(item).strip())


def _framework(headers: dict[str, Any]) -> str | None:
    text = " ".join(str(headers.get(key) or headers.get(key.title()) or "") for key in ("server", "x-powered-by")).casefold()
    for marker, name in (("flask", "Flask"), ("express", "Express"), ("php", "PHP"), ("nginx", "Nginx"), ("apache", "Apache")):
        if marker in text:
            return name
    return None


def _jwt_detected(body: str, cookie_names: tuple[str, ...]) -> bool:
    folded = body.casefold()
    return "jwt" in folded or "eyj" in folded or any("jwt" in name.casefold() for name in cookie_names)


def _links(body: str, base_url: str) -> list[str]:
    values = re.findall(r"<a\b[^>]*?href=[\"']([^\"']+)", body, re.I)
    result: list[str] = []
    origin = urlparse(base_url).netloc
    for value in values:
        candidate = urljoin(base_url, value)
        if urlparse(candidate).netloc == origin and candidate not in result:
            result.append(candidate)
    return result[:20]


def _summary(body: str, status: int | None) -> str:
    # Preserve only a bounded textual hint. Do not persist flag-shaped values
    # or cookie/token-looking material in the graph fact.
    cleaned = BreadthScanner._FLAG_RE.sub("<flag-candidate>", body)
    return f"HTTP {status if status is not None else 'unknown'}: {cleaned[:200]}"


__all__ = ["BreadthScanner", "ReconObservation", "ReconReport"]
