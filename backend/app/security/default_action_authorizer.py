"""Default Solver action authorization policy.

This policy is intentionally narrower than the legacy security services.  It
checks only action capability, target scope, and per-run resource limits.  It
does not execute actions, inspect ORM state, or replace the Evidence Store.
Redirect hops are not authorized here; the execution backend must enforce the
same scope on every redirect or disable redirects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.solver.action import ActionIntent
from app.solver.context import RunContext, RuntimeUsage

from .action_authorizer import ActionSecurityDecision, SecurityDecisionType

SUPPORTED_ACTIONS = frozenset(
    {
        "http_request",
        "sql_boolean_compare",
        "sql_injection_probe",
        "oracle_probe_matrix",
        "oracle_expression_calibration",
        "mysql_metadata_discovery",
        "boolean_config_extract",
        "sql_extract",
        "request_capture",
        "sqlmap_detect",
        "sqlmap_run",
        "sqlite_metadata_discovery",
        "script_run",
    }
)
SUPPORTED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class _Endpoint:
    host: str
    port: int | None


def _normalized_host(host: str | None) -> str | None:
    if not host:
        return None
    normalized = host.rstrip(".").casefold()
    return normalized or None


def _parse_url(
    value: Any,
    *,
    supported_schemes: frozenset[str] = SUPPORTED_SCHEMES,
) -> _Endpoint | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme.casefold() not in supported_schemes:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = _normalized_host(parsed.hostname)
        if host is None:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    return _Endpoint(host, port)


def _parse_scope_entry(value: Any) -> _Endpoint | None:
    if not isinstance(value, str) or not value.strip():
        return None
    entry = value.strip()
    candidate = entry if "://" in entry else f"//{entry}"
    try:
        parsed = urlparse(candidate)
        if parsed.username is not None or parsed.password is not None:
            return None
        host = _normalized_host(parsed.hostname)
        if host is None:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    return _Endpoint(host, port)


def extract_action_target(action: ActionIntent, context: RunContext) -> str | None:
    """Return the URL independently targeted by a supported action.

    SQL comparison actions commonly carry only query/predicate parameters; in
    that case the Challenge target is the explicit execution target. HTTP
    requests must provide their own URL so a missing target fails closed.
    """

    parameters = action.parameters or {}
    request = parameters.get("request")
    if isinstance(request, dict):
        nested_url = request.get("url") or request.get("endpoint")
        if isinstance(nested_url, str) and nested_url.strip():
            return nested_url
    for key in ("url", "target_url", "endpoint"):
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if action.action_name in SUPPORTED_ACTIONS - {"http_request"}:
        return context.challenge.target.url
    return None


class DefaultActionAuthorizer:
    """Production default policy for the new Solver execution boundary."""

    def __init__(
        self,
        *,
        policy_id: str = "solver-default",
        supported_actions: frozenset[str] = SUPPORTED_ACTIONS,
        supported_schemes: frozenset[str] = SUPPORTED_SCHEMES,
    ) -> None:
        self.policy_id = policy_id
        self.supported_actions = frozenset(supported_actions)
        self.supported_schemes = frozenset(item.casefold() for item in supported_schemes)

    def authorize(
        self,
        action: ActionIntent,
        context: RunContext | None,
    ) -> ActionSecurityDecision:
        return self.authorize_with_usage(action, context, RuntimeUsage())

    def authorize_with_usage(
        self,
        action: ActionIntent,
        context: RunContext | None,
        usage: RuntimeUsage,
    ) -> ActionSecurityDecision:
        if action.action_name not in self.supported_actions:
            return self._deny(
                "action capability is not enabled by the default Solver policy",
                "ACTION_NOT_ALLOWED",
            )
        if context is None:
            return self._deny("run context is required for scoped execution", "CONTEXT_MISSING")

        target = extract_action_target(action, context)
        endpoint = _parse_url(target, supported_schemes=self.supported_schemes)
        if endpoint is None:
            return self._deny("action target is malformed or unsupported", "INVALID_TARGET")
        if not self._in_scope(endpoint, context):
            return self._deny("action target is outside the Challenge scope", "TARGET_OUT_OF_SCOPE")

        max_actions = context.limits.max_actions
        # max_steps and max_runtime_seconds remain outside this policy until
        # the production lifecycle supplies a durable, authoritative usage
        # source.  This adapter enforces only the reliable tool-call count.
        if max_actions is not None and usage.tool_calls >= max_actions:
            return self._deny("maximum tool-call limit has been reached", "TOOL_CALL_LIMIT_REACHED")

        return ActionSecurityDecision(
            decision=SecurityDecisionType.ALLOW,
            reason="action capability, target scope, and resource checks passed",
            policy_id=self.policy_id,
            reason_code="ALLOW",
        )

    def _in_scope(self, endpoint: _Endpoint, context: RunContext) -> bool:
        raw_scopes = tuple(context.challenge.target.allowed_hosts)
        scopes = [
            parsed
            for value in raw_scopes
            if (parsed := _parse_scope_entry(value)) is not None
        ]
        if not raw_scopes:
            target = _parse_url(
                context.challenge.target.url,
                supported_schemes=self.supported_schemes,
            )
            if target is not None:
                scopes = [target]
        return any(
            endpoint.host == scope.host
            and (scope.port is None or endpoint.port == scope.port)
            for scope in scopes
        )

    def _deny(self, reason: str, reason_code: str) -> ActionSecurityDecision:
        return ActionSecurityDecision(
            decision=SecurityDecisionType.DENY,
            reason=reason,
            policy_id=self.policy_id,
            reason_code=reason_code,
        )
