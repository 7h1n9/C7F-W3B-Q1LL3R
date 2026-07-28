"""Runner-side target allowlist checks.

This module intentionally has no policy for HTTP method or request content;
only the destination host/IP/port is compared with the Run scope.
"""

from __future__ import annotations

import ipaddress
import asyncio
import contextlib
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AllowedTarget:
    original: str
    hostname: str
    resolved_ips: frozenset[str]
    allowed_ports: frozenset[int] | None = None


def _parse(raw: str) -> tuple[str, int | None]:
    parsed = urlsplit(str(raw).strip() if "://" in str(raw).strip() else f"//{str(raw).strip()}")
    if not parsed.hostname:
        raise ValueError("missing hostname")
    return parsed.hostname.rstrip(".").lower(), parsed.port


def _resolve(hostname: str) -> frozenset[str]:
    try:
        return frozenset({str(ipaddress.ip_address(hostname))})
    except ValueError:
        try:
            return frozenset(str(ipaddress.ip_address(item[4][0])) for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM))
        except (OSError, ValueError):
            return frozenset()


def normalize_allowed_targets(values: list[str] | None) -> list[AllowedTarget]:
    result: list[AllowedTarget] = []
    for raw in values or []:
        hostname, port = _parse(raw)
        item = AllowedTarget(str(raw), hostname, _resolve(hostname), frozenset({port}) if port is not None else None)
        if item not in result:
            result.append(item)
    return result


def target_allowed(url: str, allowed_hosts: list[str] | None) -> bool:
    try:
        hostname, port = _parse(url)
    except (ValueError, TypeError):
        return False
    current = _resolve(hostname)
    for item in normalize_allowed_targets(allowed_hosts):
        if hostname != item.hostname:
            continue
        if item.allowed_ports is not None and port not in item.allowed_ports:
            continue
        if item.resolved_ips and current and not (item.resolved_ips & current):
            continue
        return True
    return False


def authorized_connect_target(raw: str, allowed_hosts: list[str] | None) -> tuple[str, int]:
    """Return a snapshotted IP/port for one authorized proxy connection."""
    hostname, port = _parse(raw)
    effective_port = int(port or (443 if str(raw).lower().startswith("https://") else 80))
    targets = normalize_allowed_targets(allowed_hosts)
    for item in targets:
        if item.hostname != hostname or (item.allowed_ports is not None and effective_port not in item.allowed_ports):
            continue
        ips = item.resolved_ips or _resolve(hostname)
        if ips:
            return sorted(ips)[0], effective_port
        return hostname, effective_port
    raise ValueError("target host or port is not allowlisted")


class TargetAllowlistProxy:
    """Small per-job HTTP/CONNECT proxy enforcing the Run target scope.

    The proxy is intentionally scoped to one Job and closes with that Job.
    It authorizes every CONNECT/request destination and connects to the
    snapshotted IP, preventing a later DNS answer from escaping the allowlist.
    """

    def __init__(self, allowed_hosts: list[str] | None) -> None:
        self.allowed_hosts = list(allowed_hosts or [])
        self.server: asyncio.AbstractServer | None = None

    @property
    def url(self) -> str:
        if self.server is None or not self.server.sockets:
            raise RuntimeError("proxy is not started")
        port = int(self.server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(header_bytes) > 65536:
                raise ValueError("proxy headers too large")
            lines = header_bytes[:-4].decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [line for line in lines[1:] if ":" in line]
            header_map = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip() for line in headers}
            if method.upper() == "CONNECT":
                host, port_text = target.rsplit(":", 1)
                port = int(port_text)
                ip, _ = authorized_connect_target(f"http://{host}:{port}/", self.allowed_hosts)
                upstream = await asyncio.open_connection(ip, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._tunnel(reader, writer, upstream[0], upstream[1])
                return
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"}:
                host_header = header_map.get("host")
                if not host_header:
                    raise ValueError("proxy request has no host")
                target = f"http://{host_header}{target}"
                parsed = urlsplit(target)
            ip, port = authorized_connect_target(target, self.allowed_hosts)
            upstream_reader, upstream_writer = await asyncio.open_connection(ip, port)
            content_length = int(header_map.get("content-length", "0") or 0)
            body = await reader.readexactly(content_length) if content_length > 0 else b""
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            outbound = [f"{method} {path} HTTP/1.1"]
            for line in headers:
                key = line.split(":", 1)[0].strip().lower()
                if key not in {"proxy-connection", "connection", "keep-alive"}:
                    outbound.append(line)
            outbound.append("Connection: close")
            upstream_writer.write(("\r\n".join(outbound) + "\r\n\r\n").encode("latin-1") + body)
            await upstream_writer.drain()
            while chunk := await upstream_reader.read(65536):
                writer.write(chunk)
                await writer.drain()
            upstream_writer.close()
            await upstream_writer.wait_closed()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError, ValueError):
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, upstream_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter) -> None:
        async def forward(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
            try:
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    destination.close()

        await asyncio.gather(forward(reader, upstream_writer), forward(upstream_reader, writer))


def enforced_proxy_available() -> bool:
    return True
