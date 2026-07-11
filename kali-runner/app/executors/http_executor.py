from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest


async def execute_http(request: JobRequest) -> dict:
    args = request.arguments
    url = str(args.get("url", ""))
    def validate(candidate: str) -> None:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() not in request.allowed_hosts or parsed.hostname == "169.254.169.254":
            raise HTTPException(403, detail="target host is not allowlisted")
    validate(url)
    follow = bool(args.get("follow_redirects", False))
    async with httpx.AsyncClient(follow_redirects=False, timeout=min(float(args.get("timeout", 30)), settings.job_timeout_seconds), trust_env=False) as client:
        for hop in range(6):
            response = await client.request(method=str(args.get("method", "GET")).upper(), url=url, headers=args.get("headers", {}), params=args.get("query", {}), content=args.get("body"))
            location = response.headers.get("location")
            if not location or response.status_code not in {301, 302, 303, 307, 308}:
                break
            if not follow:
                break
            if hop == 5:
                raise HTTPException(400, detail="too many redirects")
            url = urljoin(str(response.url), location)
            validate(url)
    body = response.content[:settings.max_output_bytes]
    return {"status_code": response.status_code, "headers": dict(response.headers), "body": body.decode(errors="replace"), "truncated": len(response.content) > len(body), "summary": f"HTTP {response.status_code} from {urlparse(str(response.url)).hostname}"}
