from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
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
    disclosed_paths: tuple[str, ...] = ()
    form_actions: tuple[str, ...] = ()
    parameter_names: tuple[str, ...] = ()
    content_type: str | None = None
    json_keys: tuple[str, ...] = ()
    form_methods: tuple[str, ...] = ()
    form_enctypes: tuple[str, ...] = ()
    xml_detected: bool = False
    multipart_detected: bool = False


@dataclass(frozen=True, slots=True)
class ReconReport:
    observations: tuple[ReconObservation, ...]
    endpoints: tuple[str, ...]
    auth_required: bool
    session_cookie_names: tuple[str, ...]
    frameworks: tuple[str, ...]
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    public_credentials: tuple[str, str] | None = None


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

    def __init__(self, execute_tool: ToolExecutor, *, max_requests: int = 20) -> None:
        self.execute_tool = execute_tool
        self.max_requests = max(4, int(max_requests))

    async def scan(self, *, base_url: str, workspace_id: str, run_id: str, session_name: str = "muteki-recon") -> ReconReport:
        observations: list[ReconObservation] = []
        seen: set[str] = set()
        evidence_refs: list[str] = []
        public_credentials: tuple[str, str] | None = None

        async def request(url: str) -> None:
            nonlocal public_credentials
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
            body = _response_body(output, workspace_id)
            discovered_credentials = _public_demo_credentials(body)
            if discovered_credentials:
                public_credentials = discovered_credentials
            extracted = output.get("extracted_facts") if isinstance(output.get("extracted_facts"), dict) else {}
            form_actions = extracted.get("form_actions") or output.get("form_actions") or []
            parameter_names = extracted.get("parameter_names") or output.get("parameter_names") or []
            headers = output.get("headers") if isinstance(output.get("headers"), dict) else {}
            content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")[:120] or None
            cookie_names = _cookie_names(output)
            location = str(headers.get("location") or headers.get("Location") or "")
            final_url = str(output.get("final_url") or "")
            redirected_to_login = any("login" in value.casefold() for value in (location, final_url))
            framework = _framework(headers)
            jwt_detected = _jwt_detected(body, cookie_names)
            links = tuple(_links(body, normalized))
            disclosed_paths = tuple(_disclosed_paths(body))
            parsed_forms = _forms(body, normalized)
            form_actions = parsed_forms[0] or form_actions
            form_methods = parsed_forms[1]
            form_enctypes = parsed_forms[2]
            if not parameter_names:
                parameter_names = _field_names(body)
            json_keys = _json_keys(body, content_type)
            xml_detected = bool(re.search(r"<!doctype\s+[^>]*xml|application/xml|text/xml", body, re.I) or "xml" in (content_type or "").casefold())
            multipart_detected = bool(re.search(r"multipart/form-data", body, re.I) or "multipart/form-data" in (" ".join(form_enctypes)).casefold() or "multipart/form-data" in (content_type or "").casefold())
            refs = tuple(str(item) for item in getattr(result, "evidence_refs", ()) or ())
            evidence_refs.extend(refs)
            auth_hint = _auth_hint(body, form_actions, parameter_names)
            observations.append(ReconObservation(
                normalized,
                status,
                _summary(body, status),
                cookie_names,
                redirected_to_login or auth_hint,
                framework,
                jwt_detected,
                links,
                refs,
                disclosed_paths,
                tuple(str(item) for item in form_actions or ()),
                tuple(str(item) for item in parameter_names or ()),
                content_type,
                tuple(json_keys),
                tuple(form_methods),
                tuple(form_enctypes),
                xml_detected,
                multipart_detected,
            ))

        # Session creation is not counted as a target request.
        await self.execute_tool("http_session_request", {"operation": "create", "session_name": session_name}, workspace_id, run_id)
        for path in self._COMMON_PATHS:
            await request(path)
        # Follow same-host links to a bounded second level. This is needed for
        # document portals where the preview URL is only present on a detail
        # page, while the request cap still prevents an open crawler.
        queue = [link for item in observations for link in item.links]
        cursor = 0
        while cursor < len(queue) and len(observations) < self.max_requests:
            link = queue[cursor]
            cursor += 1
            before = len(observations)
            await request(link)
            if len(observations) > before:
                queue.extend(observations[-1].links)

        endpoints = tuple(dict.fromkeys(item.endpoint for item in observations if item.status_code != 404 or item.endpoint.endswith("/")))
        cookies = tuple(dict.fromkeys(cookie for item in observations for cookie in item.cookie_names))
        frameworks = tuple(dict.fromkeys(item.framework for item in observations if item.framework))
        auth_required = any(
            item.status_code in {401, 403}
            or item.redirected_to_login
            or any(marker in item.summary.casefold() for marker in ("login", "sign in", "unauthorized"))
            for item in observations
        )
        return ReconReport(tuple(observations), endpoints, auth_required, cookies, frameworks, tuple(dict.fromkeys(evidence_refs)), public_credentials)


def _nested(value: dict[str, Any], key: str, child: str) -> Any:
    nested = value.get(key)
    return nested.get(child) if isinstance(nested, dict) else None


def _response_body(output: dict[str, Any], workspace_id: str = "") -> str:
    """Read the bounded HTML excerpt from either Gateway result shape.

    The production GatewayWorker normally flattens ``model_view`` into
    ``body_excerpt``.  During event/replay and some Runner versions the same
    excerpt remains nested, so Race must accept both shapes without retaining
    a raw response beyond the current observation.
    """

    direct = output.get("body_excerpt") or output.get("body") or output.get("content_excerpt")
    if isinstance(direct, str) and direct:
        return direct
    for key in ("model_view", "structured_result"):
        nested = output.get(key)
        if not isinstance(nested, dict):
            continue
        candidate = nested.get("content_excerpt") or nested.get("body_excerpt") or nested.get("body")
        if isinstance(candidate, str) and candidate:
            return candidate
        view = nested.get("model_view")
        if isinstance(view, dict):
            candidate = view.get("content_excerpt") or view.get("body_excerpt")
            if isinstance(candidate, str) and candidate:
                return candidate
    def nested_excerpt(value: Any, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(value, dict):
            for key in ("body_excerpt", "content_excerpt", "body"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
            for child in value.values():
                found = nested_excerpt(child, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value[:20]:
                found = nested_excerpt(child, depth + 1)
                if found:
                    return found
        return ""

    found = nested_excerpt(output)
    if found:
        return found
    # A Runner-backed Gateway may return only the model view while the full
    # bounded response is available in the already-authorized local artifact.
    # Read it transiently for Race classification; never copy it into Graph
    # facts or the ReconReport.
    relative_paths: list[str] = []
    artifact_path = output.get("artifact_path")
    if isinstance(artifact_path, str):
        relative_paths.append(artifact_path)
    for artifact in output.get("artifacts", ()) if isinstance(output.get("artifacts"), list) else ():
        if isinstance(artifact, dict):
            relative = artifact.get("relative_path") or artifact.get("path")
            if isinstance(relative, str):
                relative_paths.append(relative)
    root = Path(workspace_id).resolve() if workspace_id else None
    if root is not None:
        for relative in relative_paths:
            candidate = (root / relative).resolve()
            if root not in candidate.parents or not candidate.is_file():
                continue
            try:
                artifact_text = candidate.read_text(encoding="utf-8", errors="replace")[:1_000_000]
                artifact_value = json.loads(artifact_text)
            except (OSError, ValueError, UnicodeError):
                continue
            if isinstance(artifact_value, dict):
                candidate_body = _response_body(artifact_value)
                if candidate_body:
                    return candidate_body
    return str(output.get("summary") or "")


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


def _auth_hint(body: str, form_actions: Any, parameter_names: Any) -> bool:
    """Recognize a public login form without persisting credentials."""

    text = body.casefold()
    forms = " ".join(str(item) for item in form_actions or ()).casefold()
    names = " ".join(str(item) for item in parameter_names or ()).casefold()
    return "password" in text or "/login" in forms or {"username", "password"}.issubset(set(names.split()))


def _public_demo_credentials(body: str) -> tuple[str, str] | None:
    """Extract only credentials explicitly exposed by the public challenge page."""

    # Challenge pages intentionally publish demo credentials in adjacent
    # ``<code>user</code> / <code>password</code>`` elements.  Read only that
    # public presentation form; never infer, brute-force, or persist secrets.
    code_values = [
        re.sub(r"\s+", " ", value).strip()
        for value in re.findall(r"<code\b[^>]*>([^<]{1,120})</code>", body, re.I)
    ]
    for index in range(len(code_values) - 1):
        username, password = code_values[index:index + 2]
        if (
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", username)
            and len(password) >= 4
            and any(marker in password.casefold() for marker in ("pass", "pwd"))
        ):
            return username, password

    folded = body.casefold()
    if "demo" in folded and "demo-pass" in folded:
        return "demo", "demo-pass"
    return None


def _disclosed_paths(body: str) -> list[str]:
    """Extract non-secret archive path references disclosed by a page."""

    values = re.findall(r"(?:legacy_archive_ref|archive_ref|document_path)\s*:\s*([A-Za-z0-9._/-]+)", body, re.I)
    return list(dict.fromkeys(str(item)[:300] for item in values))[:10]


def _forms(body: str, base_url: str) -> tuple[list[str], list[str], list[str]]:
    actions: list[str] = []
    methods: list[str] = []
    enctypes: list[str] = []
    for tag in re.findall(r"<form\b[^>]*>", body, re.I)[:20]:
        action = re.search(r"\baction=[\"']([^\"']+)", tag, re.I)
        method = re.search(r"\bmethod=[\"']([^\"']+)", tag, re.I)
        enctype = re.search(r"\benctype=[\"']([^\"']+)", tag, re.I)
        if action:
            actions.append(urljoin(base_url, action.group(1)))
        if method:
            methods.append(method.group(1).upper()[:10])
        if enctype:
            enctypes.append(enctype.group(1).casefold()[:80])
    return list(dict.fromkeys(actions)), list(dict.fromkeys(methods)), list(dict.fromkeys(enctypes))


def _field_names(body: str) -> list[str]:
    values = re.findall(r"<(?:input|textarea|select)\b[^>]*\bname=[\"']([A-Za-z][A-Za-z0-9_.-]{0,79})", body, re.I)
    return list(dict.fromkeys(values))[:40]


def _json_keys(body: str, content_type: str | None) -> list[str]:
    if not content_type or "json" not in content_type.casefold():
        return []
    try:
        import json

        parsed = json.loads(body)
    except (TypeError, ValueError):
        return []
    return [str(key) for key in parsed if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", str(key))][:40] if isinstance(parsed, dict) else []


__all__ = ["BreadthScanner", "ReconObservation", "ReconReport"]
