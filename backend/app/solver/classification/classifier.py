from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class VulnerabilityClassifier:
    """Classify likely vulnerability families using local, explainable signals.

    This classifier deliberately has no model or tool dependency.  It creates
    hypotheses only; verification remains the responsibility of the Solver
    evidence and completion layers.
    """

    _ORDER = (
        "SQLInjection",
        "FileUpload",
        "XSS",
        "SSRF",
        "CommandInjection",
        "PrivilegeBypass",
        "JWT",
        "InfoDisclosure",
    )

    def classify(
        self,
        challenge_context: Any,
        initial_response: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        keys, values = self._flatten(challenge_context)
        response_keys, response_values = self._flatten(initial_response or {})
        keys.update(response_keys)
        values.extend(response_values)
        text = " ".join([*keys, *values]).lower()
        response_text = " ".join(response_values).lower()
        scores: dict[str, float] = {name: 0.0 for name in self._ORDER}
        reasons: dict[str, list[str]] = {name: [] for name in self._ORDER}

        def add(name: str, score: float, reason: str) -> None:
            scores[name] += score
            reasons[name].append(reason)

        parameter_names = " ".join(sorted(keys))
        if re.search(r"(?:^|[._\-\s])(id|user|search|query|keyword|asset_no)(?:$|[._\-\s])", parameter_names):
            add("SQLInjection", 0.42, "SQL-like parameter name")
        if re.search(r"sql syntax|mysql|sqlite|postgres|database error|near .* syntax|sqlstate", text):
            add("SQLInjection", 0.48, "database or SQL error signal")
        if re.search(r"(?:^|\s)(?:\d+)(?:\s|$)", " ".join(values)) and any(
            marker in keys for marker in ("id", "user", "search", "query", "asset_no")
        ):
            add("SQLInjection", 0.14, "numeric input at a request parameter")

        if re.search(r"multipart/form-data|enctype\s*=\s*[\"']?multipart", text):
            add("FileUpload", 0.55, "multipart upload signal")
        if re.search(r"(?:file|upload|image|avatar|attachment)", parameter_names):
            add("FileUpload", 0.38, "file-oriented parameter name")

        submitted = [item.lower() for item in values if item]
        reflected = any(item in response_text for item in submitted if len(item) >= 3)
        if reflected:
            add("XSS", 0.46, "input appears reflected in the response")
        if re.search(r"<script\b|text/html|<input\b|html context", text):
            add("XSS", 0.34, "HTML or script context")

        if re.search(r"(?:^|[._\-\s])(url|src|path|dest|redirect)(?:$|[._\-\s])", parameter_names):
            add("SSRF", 0.44, "URL-fetch parameter name")
        if re.search(r"fetch(?:es)?\s+(?:an?\s+)?external|external\s+url|server[- ]side request", text):
            add("SSRF", 0.45, "external fetch signal")

        if re.search(r"(?:^|[._\-\s])(ping|ip|host|exec|system|command)(?:$|[._\-\s])", parameter_names):
            add("CommandInjection", 0.52, "command-oriented parameter name")
        if re.search(r"command execution|shell command|os\.system|subprocess", text):
            add("CommandInjection", 0.42, "command execution signal")

        if re.search(r"(?:^|[._\-\s])(admin|role|user|userid)(?:$|[._\-\s])", parameter_names):
            add("PrivilegeBypass", 0.32, "role or identity parameter")
        if re.search(r"/admin(?:/|\b)|admin api|idor|access control", text):
            add("PrivilegeBypass", 0.48, "administrative or access-control surface")

        if re.search(r"authorization\s*[:=]\s*bearer|bearer\s+[a-z0-9._-]+", text):
            add("JWT", 0.52, "Bearer authorization token")
        if re.search(r"(?:cookie|header).*(?:token|jwt|session)|(?:token|jwt|session).*cookie", text):
            add("JWT", 0.35, "session token signal")

        if re.search(r"directory listing|index of /|source code|debug|traceback|stack trace|error stack", text):
            add("InfoDisclosure", 0.55, "source, directory, or debug disclosure")
        if re.search(r"(?:\.git/|/source|/debug|server version)", text):
            add("InfoDisclosure", 0.38, "sensitive path or version disclosure")

        evidence_refs = [
            str(item)
            for item in (initial_response or {}).get("evidence_refs", [])
            if str(item)
        ]
        hypotheses = []
        for name in self._ORDER:
            confidence = min(0.99, round(scores[name], 2))
            if confidence <= 0:
                continue
            hypotheses.append(
                {
                    "type": name,
                    "confidence": confidence,
                    "reason": "; ".join(reasons[name]),
                    "evidence_refs": evidence_refs,
                    "tested": False,
                    "failed_attempts": 0,
                }
            )
        return sorted(hypotheses, key=lambda item: (-item["confidence"], item["type"]))

    @staticmethod
    def _flatten(value: Any, prefix: str = "") -> tuple[set[str], list[str]]:
        keys: set[str] = set()
        values: list[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized:
                    keys.add(normalized)
                child_keys, child_values = VulnerabilityClassifier._flatten(item, normalized)
                keys.update(child_keys)
                values.extend(child_values)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                child_keys, child_values = VulnerabilityClassifier._flatten(item, prefix)
                keys.update(child_keys)
                values.extend(child_values)
        elif value is not None:
            values.append(str(value).strip())
        return keys, values
