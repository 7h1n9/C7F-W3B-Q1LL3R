from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .classifier import VulnerabilityClassifier


class LLMClassifierError(RuntimeError):
    """Raised when an enabled LLM classifier cannot produce valid hypotheses."""


@dataclass(frozen=True)
class LLMClassifierConfig:
    """Runtime configuration for the optional Solver classification provider."""

    use_llm: bool = True
    timeout_seconds: int = 10
    fallback_to_heuristic: bool = True
    endpoint: str = ""
    api_key: str = ""
    model: str = ""

    @classmethod
    def from_env(cls) -> "LLMClassifierConfig":
        """Read both unprefixed and existing ``APP_`` environment conventions."""
        return cls(
            use_llm=_env_bool("CLASSIFIER_USE_LLM", True),
            timeout_seconds=max(1, _env_int("CLASSIFIER_LLM_TIMEOUT", 10)),
            fallback_to_heuristic=_env_bool("CLASSIFIER_FALLBACK_TO_HEURISTIC", True),
            endpoint=_env_value("CLASSIFIER_LLM_URL", ""),
            api_key=_env_value("CLASSIFIER_LLM_API_KEY", ""),
            model=_env_value("CLASSIFIER_LLM_MODEL", ""),
        )


def _env_value(name: str, default: str) -> str:
    return os.getenv(name, os.getenv(f"APP_{name}", default)).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, os.getenv(f"APP_{name}"))
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, os.getenv(f"APP_{name}"))
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


class LLMVulnerabilityClassifier:
    """Use an optional structured LLM classifier with deterministic fallback.

    The provider sees only the allowlisted challenge context and initial
    response supplied by the Solver. It never receives Run ORM objects,
    Evidence Store records, secrets, or challenge ground truth.
    """

    ALIASES = {
        "SQL_INJECTION": "SQLInjection",
        "SQLINJECTION": "SQLInjection",
        "FILE_UPLOAD": "FileUpload",
        "FILEUPLOAD": "FileUpload",
        "CROSS_SITE_SCRIPTING": "XSS",
        "COMMAND_INJECTION": "CommandInjection",
        "COMMANDINJECTION": "CommandInjection",
        "PRIVILEGE_BYPASS": "PrivilegeBypass",
        "PRIVILEGEBYPASS": "PrivilegeBypass",
        "INFORMATION_DISCLOSURE": "InfoDisclosure",
        "INFODISCLOSURE": "InfoDisclosure",
    }
    ALLOWED_TYPES = frozenset(
        {
            "SQLInjection",
            "FileUpload",
            "XSS",
            "SSRF",
            "CommandInjection",
            "PrivilegeBypass",
            "JWT",
            "InfoDisclosure",
        }
    )

    def __init__(
        self,
        *,
        config: LLMClassifierConfig | None = None,
        heuristic_classifier: VulnerabilityClassifier | None = None,
        client: Callable[..., Any] | Any | None = None,
        llm_client: Callable[..., Any] | Any | None = None,
    ) -> None:
        self.config = config or LLMClassifierConfig.from_env()
        self.heuristic_classifier = heuristic_classifier or VulnerabilityClassifier()
        self.client = client if client is not None else llm_client
        self.last_source = "none"
        self.last_error_code: str | None = None

    async def classify(
        self,
        challenge_context: Any,
        initial_response: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Return normalized hypotheses, falling back on provider failure."""
        if not self.config.use_llm:
            return self._heuristic(challenge_context, initial_response)
        try:
            raw = await asyncio.wait_for(
                self._request(challenge_context, initial_response or {}),
                timeout=self.config.timeout_seconds,
            )
            hypotheses = self._normalize(raw)
            if not hypotheses:
                raise LLMClassifierError("LLM_CLASSIFIER_EMPTY_RESPONSE")
            self.last_source = "llm"
            self.last_error_code = None
            return hypotheses
        except Exception as error:
            self.last_source = "heuristic" if self.config.fallback_to_heuristic else "failed"
            self.last_error_code = type(error).__name__
            if not self.config.fallback_to_heuristic:
                if isinstance(error, LLMClassifierError):
                    raise
                raise LLMClassifierError("LLM_CLASSIFIER_UNAVAILABLE") from error
            return self._heuristic(challenge_context, initial_response)

    def _heuristic(
        self,
        challenge_context: Any,
        initial_response: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return self.heuristic_classifier.classify(challenge_context, initial_response or {})

    async def _request(
        self,
        challenge_context: Any,
        initial_response: Mapping[str, Any],
    ) -> Any:
        request = self._request_payload(challenge_context, initial_response)
        if self.client is not None:
            return await self._call_injected_client(request)
        if not self.config.endpoint:
            raise LLMClassifierError("LLM_CLASSIFIER_ENDPOINT_NOT_CONFIGURED")
        endpoint = self.config.endpoint.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {self.config.api_key}"}
                if self.config.api_key
                else {},
                json=request,
            )
            response.raise_for_status()
            return response.json()

    async def _call_injected_client(self, request: dict[str, Any]) -> Any:
        client = self.client
        if callable(client):
            try:
                result = client(request, timeout=self.config.timeout_seconds)
            except TypeError:
                result = client(request)
        elif callable(getattr(client, "complete", None)):
            result = client.complete(request)
        elif callable(getattr(client, "post", None)):
            result = client.post(
                self.config.endpoint,
                json=request,
                timeout=self.config.timeout_seconds,
            )
        else:
            raise LLMClassifierError("LLM_CLASSIFIER_CLIENT_INVALID")
        if isinstance(result, Awaitable):
            result = await result
        if hasattr(result, "raise_for_status"):
            result.raise_for_status()
        if hasattr(result, "json") and callable(result.json):
            return result.json()
        return result

    def _request_payload(
        self,
        challenge_context: Any,
        initial_response: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = _safe_context(challenge_context)
        response = _safe_context(initial_response)
        return {
            "model": self.config.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Classify vulnerability hypotheses. Return JSON only with "
                        "{\"hypotheses\":[{\"type\":\"...\",\"confidence\":0.0," 
                        "\"reason\":\"...\",\"evidence_refs\":[],\"tested\":false,"
                        "\"failed_attempts\":0}]} . Use only the supported types."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"challenge_context": context, "initial_response": response},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                },
            ],
        }

    def _normalize(self, raw: Any) -> list[dict[str, Any]]:
        payload = self._extract_payload(raw)
        items = payload.get("hypotheses") if isinstance(payload, Mapping) else payload
        if not isinstance(items, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            raw_type = str(item.get("type") or item.get("vulnerability_type") or "").strip()
            vulnerability_type = self.ALIASES.get(raw_type.upper(), raw_type)
            if vulnerability_type not in self.ALLOWED_TYPES:
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
            except (TypeError, ValueError):
                continue
            raw_evidence_refs = item.get("evidence_refs", [])
            evidence_refs = (
                [str(ref) for ref in raw_evidence_refs if str(ref)]
                if isinstance(raw_evidence_refs, (list, tuple, set, frozenset))
                else []
            )
            try:
                failed_attempts = max(0, int(item.get("failed_attempts", 0) or 0))
            except (TypeError, ValueError):
                failed_attempts = 0
            normalized.append(
                {
                    "type": vulnerability_type,
                    "confidence": round(confidence, 2),
                    "reason": str(item.get("reason") or "LLM classification signal"),
                    "evidence_refs": evidence_refs,
                    "tested": bool(item.get("tested", False)),
                    "failed_attempts": failed_attempts,
                }
            )
        return sorted(normalized, key=lambda item: (-item["confidence"], item["type"]))

    @classmethod
    def _extract_payload(cls, raw: Any) -> Any:
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text
                text = text.rsplit("```", 1)[0].strip()
            return json.loads(text)
        if isinstance(raw, Mapping):
            if "hypotheses" in raw:
                return raw
            choices = raw.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                message = choices[0].get("message")
                if isinstance(message, Mapping):
                    return cls._extract_payload(message.get("content", ""))
            if "content" in raw:
                return cls._extract_payload(raw["content"])
        return raw


def _safe_context(value: Any) -> Any:
    """Project dataclasses/mappings to JSON-safe, non-secret classifier input."""
    if isinstance(value, Mapping):
        return {str(key): _safe_context(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_context(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _safe_context(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
