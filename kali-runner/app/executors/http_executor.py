import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.executors.session_store import session_store


class _HTMLSummary(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.comments: list[str] = []
        self.links: list[str] = []
        self.scripts: list[str] = []
        self.forms: list[dict] = []
        self._title = False
        self._title_parts: list[str] = []
        self._form: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._title = True
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        elif tag == "script" and values.get("src"):
            self.scripts.append(values["src"] or "")
        elif tag == "form":
            self._form = {"action": values.get("action", ""), "method": values.get("method", "GET").upper(), "inputs": []}
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form["inputs"].append({"name": values.get("name"), "type": values.get("type", "text"), "hidden": values.get("type") == "hidden"})

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title = False
            self.title = "".join(self._title_parts).strip()
        elif tag == "form":
            self._form = None

    def handle_data(self, data: str) -> None:
        if self._title:
            self._title_parts.append(data)

    def handle_comment(self, data: str) -> None:
        if data.strip():
            self.comments.append(data.strip()[:500])


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
    return {key: ("<redacted>" if key.lower() in blocked else value[:500]) for key, value in headers.items() if key.lower() in {"content-type", "location", "server", "x-powered-by", "www-authenticate"}}


def _extract_body(body: str, content_type: str) -> dict:
    facts: dict = {"json_keys": [], "suspected_credentials": [], "suspected_flags": []}
    if "json" in content_type.lower():
        try:
            value = json.loads(body)
            facts["json_keys"] = list(value.keys())[:100] if isinstance(value, dict) else []
        except json.JSONDecodeError:
            pass
    if "html" in content_type.lower() or "<html" in body.lower():
        parser = _HTMLSummary()
        parser.feed(body)
        facts.update({"html_title": parser.title, "html_comments": parser.comments[:20], "forms": parser.forms[:20], "links": parser.links[:50], "script_urls": parser.scripts[:50]})
        facts["form_actions"] = [item["action"] for item in parser.forms]
        facts["parameter_names"] = [field.get("name") for form in parser.forms for field in form.get("inputs", []) if field.get("name")]
    lowered = body.lower()
    facts["suspected_credentials"] = [term for term in ("password", "passwd", "secret", "token", "api_key", "authorization") if term in lowered]
    facts["suspected_flags"] = re.findall(r"flag\{[^}]{1,200}\}", body, flags=re.I)[:20]
    return facts


def normalize_query_pairs(query: object) -> list[tuple[str, str]]:
    if query is None:
        return []
    if isinstance(query, dict):
        items = query.items()
    elif isinstance(query, (list, tuple)):
        items = query
    else:
        raise ValueError("query must be a mapping or list of pairs")
    pairs: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, tuple | list) and len(item) == 2:
            key, value = item
        else:
            key, value = item
        if isinstance(value, (list, tuple)):
            pairs.extend((str(key), str(part)) for part in value)
        else:
            pairs.append((str(key), "" if value is None else str(value)))
    return pairs


def merge_url_query(url: str, query: object) -> str:
    """Merge query pairs explicitly; supplied keys replace existing keys."""
    parsed = urlsplit(url)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    supplied = normalize_query_pairs(query)
    if query is None:
        merged = existing
    else:
        supplied_keys = {key for key, _ in supplied}
        merged = [pair for pair in existing if pair[0] not in supplied_keys] + supplied
    return urlunsplit(parsed._replace(query=urlencode(merged, doseq=True)))


def _request_kwargs(args: dict) -> dict:
    """Map the structured request body to the matching HTTPX argument.

    ``params=None`` is intentional: HTTPX then preserves query parameters
    already present in the URL, while a supplied query mapping is merged with
    them.  Lists are retained so repeated query parameters remain possible.
    """
    query = args.get("query")
    kwargs: dict = {"params": query if query is not None else None}
    if args.get("json") is not None:
        kwargs["json"] = args["json"]
    elif args.get("form") is not None:
        kwargs["data"] = args["form"]
    elif args.get("body") is not None:
        kwargs["content"] = args["body"]
    return kwargs


async def execute_http(request: JobRequest) -> dict:
    args = request.arguments
    url = str(args.get("url", ""))
    def validate(candidate: str) -> None:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() not in request.allowed_hosts or parsed.hostname == "169.254.169.254":
            raise HTTPException(403, detail="target host is not allowlisted")
    is_session = request.tool == "http_session_request"
    session_name = str(args.get("session_name") or "")
    if is_session and not session_name:
        raise HTTPException(422, detail="session_name is required for http_session_request")
    operation = str(args.get("operation") or "request").lower()
    if is_session and operation == "inspect":
        return {"summary": "HTTP session inspected", "extracted_facts": session_store.inspect(request.run_id, session_name)}
    if is_session and operation == "clear":
        session_store.clear(request.run_id, session_name)
        return {"summary": "HTTP session cleared", "extracted_facts": {"exists": False, "cookie_names": [], "authenticated": False}}
    if is_session and operation == "create":
        session_store.cookies(request.run_id, session_name)
        return {"summary": "HTTP session created", "extracted_facts": session_store.inspect(request.run_id, session_name)}
    if is_session and operation not in {"request", "create"}:
        raise HTTPException(422, detail="unsupported session operation")
    if is_session and (not args.get("url") or not args.get("method")):
        raise HTTPException(422, detail="method and url are required for a session request")
    validate(url)
    url = merge_url_query(url, args.get("query"))
    args = {**args, "query": None}
    follow = bool(args.get("follow_redirects", False))
    redirect_history: list[dict] = []
    request_method = str(args.get("method", "GET")).upper()
    request_headers = dict(args.get("headers", {}))
    request_cookies = session_store.cookies(request.run_id, session_name) if is_session else None
    async with httpx.AsyncClient(follow_redirects=False, timeout=min(float(args.get("timeout", 30)), settings.job_timeout_seconds), trust_env=False, cookies=request_cookies) as client:
        for hop in range(6):
            response = await client.request(
                method=request_method,
                url=url,
                headers=request_headers,
                **_request_kwargs(args),
            )
            if is_session:
                session_store.update(request.run_id, session_name, response)
            location = response.headers.get("location")
            redirect_history.append({"status_code": response.status_code, "url": str(response.url), "location": location})
            if not location or response.status_code not in {301, 302, 303, 307, 308}:
                break
            if not follow:
                break
            if hop == 5:
                raise HTTPException(400, detail="too many redirects")
            url = urljoin(str(response.url), location)
            validate(url)
            if response.status_code in {301, 302} and request_method not in {"GET", "HEAD"}:
                request_method = "GET"
                args = {**args, "body": None, "json": None, "form": None}
                request_headers = {key: value for key, value in request_headers.items() if key.lower() not in {"content-length", "content-type", "transfer-encoding"}}
            elif response.status_code == 303 and request_method != "HEAD":
                request_method = "GET"
                args = {**args, "body": None, "json": None, "form": None}
                request_headers = {key: value for key, value in request_headers.items() if key.lower() not in {"content-length", "content-type", "transfer-encoding"}}
    body = response.content[: settings.http_excerpt_bytes]
    content_type = response.headers.get("content-type", "")
    body_text = body.decode(errors="replace") if not content_type.startswith(("image/", "audio/", "video/", "application/octet-stream")) else None
    extracted = _extract_body(body_text or "", content_type) if body_text is not None else {"binary": True}
    return {
        "status_code": response.status_code,
        "final_url": str(response.url),
        "redirect_history": redirect_history[:-1],
        "content_type": content_type,
        "headers": _safe_headers(dict(response.headers)),
        "selected_headers": _safe_headers(dict(response.headers)),
        "cookie_names": [part.split("=", 1)[0].strip() for header in response.headers.get_list("set-cookie") for part in [header] if "=" in part],
        "body": body_text or "",
        "body_excerpt": body_text,
        "body_length": len(response.content),
        "truncated": len(response.content) > len(body),
        "extracted_facts": extracted,
        "summary": f"HTTP {response.status_code} from {urlparse(str(response.url)).hostname}",
    }
