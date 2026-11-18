"""Loopback-only endpoint rules for locally reverse-proxied model providers.

A "local reverse proxy" provider points the solver at a small HTTP shim running
on the operator's own machine (for example an OpenAI-compatible facade in front
of a subscription CLI).  Two things make that a security-relevant contract
rather than a convenience setting:

* the shim has no authentication of its own, so it must never be reachable from
  the network; and
* a provider configured as local would silently become an exfiltration path if
  its ``base_url`` were later edited to a remote host.

So the rule is enforced positively: the host must be loopback, and only literal
loopback addresses (or the name ``localhost``) qualify.  Names are deliberately
NOT resolved -- DNS resolution would introduce both a network dependency at
validation time and a rebinding window between validation and use.  A private
but non-loopback range such as ``192.168.0.0/16`` is rejected on purpose: the
requirement is loopback, not "on my LAN".
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# The only hostname accepted without being a literal IP.  It is a reserved name
# that cannot be delegated, so it cannot be pointed at a remote host by DNS.
LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback_host(host: str | None) -> bool:
    """Is ``host`` a literal loopback address or the reserved name ``localhost``?"""

    value = str(host or "").strip().strip("[]").casefold()
    if not value:
        return False
    if value in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        # A hostname we do not resolve.  Refusing is the safe direction: a name
        # that is not provably loopback must not receive a local-only profile.
        return False


def is_loopback_url(url: str | None) -> bool:
    """Is ``url`` an absolute http(s) URL whose host is loopback?"""

    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        return False
    return is_loopback_host(parsed.hostname)


def require_loopback_url(url: str | None, *, field: str = "base_url") -> str:
    """Return ``url`` normalized, or raise ``ValueError`` when it is not loopback.

    The message names the offending host so a rejected save is actionable rather
    than a generic "invalid configuration".
    """

    text = str(url or "").strip()
    if not text:
        raise ValueError(f"{field} is required for a local reverse-proxy provider")
    host = urlparse(text).hostname
    if not is_loopback_url(text):
        raise ValueError(
            f"{field} must point at a loopback host (127.0.0.0/8, ::1 or localhost); "
            f"got {host or text!r}"
        )
    return text.rstrip("/")


def ensure_absolute_http_url(url: str | None) -> str | None:
    """Return ``url`` as an absolute HTTP(S) URL, or ``None`` when blank.

    An arena ground may publish an environment's address as a bare
    ``host:port``.  Everything downstream treats a target as a URL, and
    ``urlsplit`` reads a bare ``host:port`` as *scheme* ``host`` with no host at
    all -- so the value silently produces an empty hostname and an empty
    host allowlist.  Defaulting the scheme here is what keeps a bare address
    usable instead of quietly unusable.

    A value that already carries a scheme is returned unchanged, so an
    ``https://`` target is never downgraded to ``http://``.
    """

    text = str(url or "").strip()
    if not text:
        return None
    if "://" in text:
        return text
    return f"http://{text}"


def outbound_trust_env() -> bool:
    """Should outbound HTTP clients honour ``HTTP(S)_PROXY`` from the environment?

    One definition for every client that leaves the process, so the answer
    cannot drift between the arena client, the MCP client and the two protocol
    proxies.  See ``Settings.respect_proxy_env`` for why the default is ``False``
    and what the failure looks like when it is not.

    Imported lazily: ``app.core.config`` is allowed to import this module, and a
    module-level import back would close the cycle.
    """

    from app.core.config import get_settings

    return bool(get_settings().respect_proxy_env)


__all__ = [
    "LOOPBACK_NAMES",
    "ensure_absolute_http_url",
    "is_loopback_host",
    "is_loopback_url",
    "outbound_trust_env",
    "require_loopback_url",
]
