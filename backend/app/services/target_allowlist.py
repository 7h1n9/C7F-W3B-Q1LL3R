"""Run-scoped target allowlist normalization.

The allowlist is deliberately about host/IP/port only.  It never rewrites a
request method, path, query, body, headers, cookies, or TLS name.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.exceptions import DomainError


@dataclass(frozen=True)
class AllowedTarget:
    original: str
    hostname: str
    resolved_ips: frozenset[str]
    allowed_ports: frozenset[int] | None = None


def _split(raw: str) -> tuple[str, int | None]:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("empty target")
    parsed = urlsplit(value if "://" in value else f"//{value}")
    host = parsed.hostname
    if not host:
        raise ValueError("target hostname is missing")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("target port is invalid") from error
    return host.rstrip(".").lower(), port


def _resolve(hostname: str) -> frozenset[str]:
    try:
        parsed = ipaddress.ip_address(hostname)
        return frozenset({str(parsed)})
    except ValueError:
        pass
    try:
        return frozenset(
            str(ipaddress.ip_address(item[4][0]))
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        )
    except (OSError, ValueError):
        # An authorized internal hostname may not be resolvable from the
        # control plane.  The Runner performs the authoritative resolution at
        # execution time, so preserving an empty set is intentional.
        return frozenset()


def normalize_allowed_targets(values: list[str] | tuple[str, ...] | None) -> list[AllowedTarget]:
    result: list[AllowedTarget] = []
    for raw in values or []:
        try:
            hostname, port = _split(str(raw))
        except ValueError as error:
            raise DomainError("TARGET_ALLOWLIST_INVALID", "An allowed target is invalid.", {"target": raw}, 422) from error
        allowed_ports = frozenset({port}) if port is not None else None
        item = AllowedTarget(str(raw), hostname, _resolve(hostname), allowed_ports)
        if item not in result:
            result.append(item)
    return result


def target_allowed(url: str, allowed: list[AllowedTarget] | list[str] | tuple[str, ...] | None) -> bool:
    try:
        hostname, port = _split(url)
    except ValueError:
        return False
    targets = normalize_allowed_targets(allowed) if not all(isinstance(item, AllowedTarget) for item in (allowed or [])) else list(allowed or [])
    for item in targets:
        if hostname != item.hostname:
            continue
        if item.allowed_ports is not None and port not in item.allowed_ports:
            continue
        # For a hostname, resolve the requested name at validation time and
        # require the address to remain within the snapshot.  This prevents a
        # redirect/DNS rebinding from changing the destination underneath a
        # still-authorized hostname.  Unresolvable lab names are left to the
        # actual HTTP client and remain valid by exact hostname.
        current_ips = _resolve(hostname)
        if item.resolved_ips and current_ips and not (item.resolved_ips & current_ips):
            continue
        return True
    return False


def require_target_allowed(url: str, allowed_hosts: list[str]) -> None:
    if not target_allowed(url, allowed_hosts):
        raise DomainError("TARGET_NOT_ALLOWED", "Target host/IP/port is not in this Run's allowlist.", {"url": url}, 403)
