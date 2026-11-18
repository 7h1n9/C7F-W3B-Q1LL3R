"""Environment-driven configuration for the protocol switch service.

Everything is read once at startup from the process environment so the service
can be launched as a plain process, a container, or a child of the backend
without a config file.  Defaults are deliberately safe: loopback bind, no
inbound auth (the service is meant to sit on the operator's own machine), and a
generous upstream timeout because a worker turn can legitimately take minutes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .translate import CHAT, RESPONSES, WIRES

#: Bind address defaults to loopback: the service holds the upstream API key and
#: must not be exposed on the network unless the operator opts in.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
#: A worker turn streams for a long time; the read timeout must outlast the
#: upstream's own turn budget rather than the 30s a normal HTTP call would use.
DEFAULT_TIMEOUT_SECONDS = 900.0


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _headers_env(name: str) -> dict[str, str]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


@dataclass
class Settings:
    """Resolved runtime configuration."""

    upstream_base_url: str = ""
    upstream_api_key: str = ""
    upstream_wire: str = CHAT
    upstream_headers: dict[str, str] = field(default_factory=dict)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    #: Optional bearer token callers must present.  Empty disables inbound auth.
    inbound_api_key: str = ""
    #: Optional model override; empty passes the caller's model through.
    default_model: str = ""
    #: Optional ``old=new,old2=new2`` model alias table.
    model_aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = env if env is not None else os.environ
        wire = str(source.get("SWITCH_UPSTREAM_WIRE") or CHAT).strip().lower()
        if wire not in WIRES:
            wire = CHAT
        return cls(
            upstream_base_url=str(source.get("SWITCH_UPSTREAM_BASE_URL") or "").strip(),
            upstream_api_key=str(source.get("SWITCH_UPSTREAM_API_KEY") or "").strip(),
            upstream_wire=wire,
            upstream_headers=_headers_env("SWITCH_UPSTREAM_HEADERS"),
            host=str(source.get("SWITCH_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST,
            port=_int_env("SWITCH_PORT", DEFAULT_PORT),
            timeout_seconds=_float_env("SWITCH_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            inbound_api_key=str(source.get("SWITCH_API_KEY") or "").strip(),
            default_model=str(source.get("SWITCH_DEFAULT_MODEL") or "").strip(),
            model_aliases=_aliases_env(source.get("SWITCH_MODEL_ALIASES")),
        )

    def map_model(self, model: str) -> str:
        """Apply the optional alias table and default override."""

        name = str(model or "").strip()
        if name and name in self.model_aliases:
            return self.model_aliases[name]
        if not name and self.default_model:
            return self.default_model
        return name

    def describe(self) -> dict[str, Any]:
        """Safe summary for ``/health`` -- never echoes the upstream key."""

        return {
            "upstream_base_url": self.upstream_base_url,
            "upstream_wire": self.upstream_wire,
            "upstream_api_key_configured": bool(self.upstream_api_key),
            "upstream_header_names": sorted(self.upstream_headers),
            "inbound_auth_required": bool(self.inbound_api_key),
            "default_model": self.default_model,
            "model_aliases": dict(self.model_aliases),
            "timeout_seconds": self.timeout_seconds,
        }


def _aliases_env(raw: Any) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    out: dict[str, str] = {}
    for pair in text.split(","):
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            out[name] = value
    return out


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_TIMEOUT_SECONDS", "Settings", "RESPONSES", "CHAT"]
