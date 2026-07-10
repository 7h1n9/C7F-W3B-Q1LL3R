from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest


async def execute_http(request: JobRequest) -> dict:
    args = request.arguments
    url = str(args.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() not in request.allowed_hosts:
        raise HTTPException(403, detail="target host is not allowlisted")
    async with httpx.AsyncClient(follow_redirects=bool(args.get("follow_redirects", False)), timeout=min(float(args.get("timeout", 30)), settings.job_timeout_seconds)) as client:
        response = await client.request(method=str(args.get("method", "GET")).upper(), url=url, headers=args.get("headers", {}), params=args.get("query", {}), content=args.get("body"))
    body = response.content[:settings.max_output_bytes]
    return {"status_code": response.status_code, "headers": dict(response.headers), "body": body.decode(errors="replace"), "truncated": len(response.content) > len(body), "summary": f"HTTP {response.status_code} from {parsed.hostname}"}
