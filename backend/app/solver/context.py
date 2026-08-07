from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "adapter",
        "dbms",
        "framework",
        "language",
        "service",
        "technology",
    }
)


def _safe_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class TargetContext:
    """Solver-safe target information extracted from a Challenge."""

    url: str | None
    allowed_hosts: tuple[str, ...]
    challenge_type: str


@dataclass(frozen=True)
class ChallengeContext:
    """Explicit allowlisted view of Challenge data available to Solver."""

    challenge_id: str
    title: str | None
    description: str
    target: TargetContext
    objective: str
    environment: Mapping[str, Any] = field(default_factory=dict)
    hints: tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", _safe_mapping(self.environment))
        object.__setattr__(self, "constraints", _safe_mapping(self.constraints))

    @classmethod
    def from_challenge(cls, challenge: Any) -> "ChallengeContext":
        challenge_type = str(getattr(challenge, "challenge_type", "WEB_TARGET") or "WEB_TARGET")
        allowed_hosts = tuple(
            str(host).strip()
            for host in (getattr(challenge, "allowed_hosts", None) or [])
            if str(host).strip()
        )
        metadata = getattr(challenge, "metadata_json", None)
        metadata = metadata if isinstance(metadata, Mapping) else {}
        environment = {
            key: metadata[key]
            for key in _SAFE_ENVIRONMENT_KEYS
            if key in metadata and isinstance(metadata[key], (str, int, float, bool))
        }
        return cls(
            challenge_id=str(getattr(challenge, "id", "") or ""),
            title=str(getattr(challenge, "name", "") or "") or None,
            description=str(getattr(challenge, "description", "") or ""),
            target=TargetContext(
                url=str(getattr(challenge, "target_url", "") or "") or None,
                allowed_hosts=allowed_hosts,
                challenge_type=challenge_type,
            ),
            objective=f"Investigate the authorized {challenge_type} challenge.",
            environment=_safe_mapping(environment),
            constraints=_safe_mapping(
                {
                    "challenge_type": challenge_type,
                    "allowed_hosts": allowed_hosts,
                }
            ),
        )


@dataclass(frozen=True)
class RunLimits:
    """Immutable execution limits projected from the existing SolveRun."""

    max_steps: int = 120
    max_actions: int | None = 120
    max_failures: int | None = None
    max_runtime_seconds: float | None = 900.0

    def __post_init__(self) -> None:
        if self.max_steps < 0:
            raise ValueError("max_steps must be non-negative")
        for name in ("max_actions", "max_failures"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds < 0:
            raise ValueError("max_runtime_seconds must be non-negative")

    @classmethod
    def from_run(cls, run: Any) -> "RunLimits":
        return cls(
            max_steps=int(getattr(run, "max_agent_steps", 120) or 0),
            max_actions=int(getattr(run, "max_tool_calls", 120) or 0),
            max_runtime_seconds=float(getattr(run, "max_runtime_seconds", 900) or 0),
        )


@dataclass(frozen=True)
class RunContext:
    """Immutable per-run context passed to the Security Boundary."""

    run_id: str
    challenge: ChallengeContext
    limits: RunLimits
    security_policy_id: str = "solver-default"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @classmethod
    def from_models(cls, run: Any, challenge: Any) -> "RunContext":
        metadata = {
            key: getattr(run, key)
            for key in ("solver_mode", "engine_type", "current_phase")
            if getattr(run, key, None) is not None
        }
        return cls(
            run_id=str(getattr(run, "id", "") or ""),
            challenge=ChallengeContext.from_challenge(challenge),
            limits=RunLimits.from_run(run),
            metadata=_safe_mapping(metadata),
        )
