"""Deterministic quality scoring for bounded TRUE/FALSE/CONTROL signals."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _profile(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _stable(profile: dict, key: str) -> bool | None:
    value = profile.get(key)
    if value is None:
        value = profile.get(f"stable_{key}")
    return value if isinstance(value, bool) else None


def _length(profile: dict) -> int | None:
    for key in ("body_length", "length", "response_length"):
        if isinstance(profile.get(key), (int, float)):
            return int(profile[key])
    return None


def score_boolean_oracle(structured: dict[str, Any]) -> dict[str, Any]:
    """Score signal quality without issuing another request.

    Missing quality dimensions are treated as unknown, never as proof of a
    stable oracle. The score is deliberately conservative and bounded.
    """
    true_profile = _profile(structured.get("true_profile") or structured.get("true_signature"))
    false_profile = _profile(structured.get("false_profile") or structured.get("false_signature"))
    control_profile = _profile(structured.get("control_profile") or structured.get("control_signature"))
    signal_features: list[str] = []
    noise_features: list[str] = []
    points = 0.0
    total = 0.0

    pairs = (("status_code_stable", "HTTP_STATUS_STABLE"), ("length_stable", "RESPONSE_LENGTH_STABLE"), ("json_stable", "JSON_FIELDS_STABLE"), ("text_stable", "KEY_TEXT_STABLE"))
    for key, label in pairs:
        observed = structured.get(key)
        if observed is None:
            observed = true_profile.get(key) if key in true_profile else false_profile.get(key)
        if isinstance(observed, bool):
            total += 1
            if observed:
                points += 1
                signal_features.append(label)
            else:
                noise_features.append(label)

    true_stable = _stable(true_profile, "stable")
    false_stable = _stable(false_profile, "stable")
    if true_stable is not None and false_stable is not None:
        total += 1
        if true_stable and false_stable:
            points += 1
            signal_features.append("MULTI_SAMPLE_STABILITY")
        else:
            noise_features.append("MULTI_SAMPLE_INSTABILITY")

    true_status = true_profile.get("status_code")
    false_status = false_profile.get("status_code")
    control_status = control_profile.get("status_code")
    separated = true_status is not None and false_status is not None and true_status != false_status
    if not separated:
        true_len, false_len = _length(true_profile), _length(false_profile)
        separated = true_len is not None and false_len is not None and true_len != false_len
    total += 1
    if separated:
        points += 1
        signal_features.append("TRUE_FALSE_SEPARABLE")
    else:
        noise_features.append("TRUE_FALSE_NOT_SEPARABLE")

    if control_profile:
        total += 1
        control_separate = control_status is None or control_status not in {true_status, false_status}
        if control_separate:
            points += 1
            signal_features.append("CONTROL_SEPARABLE")
        else:
            noise_features.append("CONTROL_COLLISION")

    if structured.get("random_fields_filtered") is True:
        total += 1
        points += 1
        signal_features.append("RANDOM_FIELDS_FILTERED")
    elif structured.get("random_fields_filtered") is False:
        total += 1
        noise_features.append("RANDOM_FIELDS_UNFILTERED")
    if structured.get("cookie_session_neutral") is True:
        total += 1
        points += 1
        signal_features.append("COOKIE_SESSION_NEUTRAL")
    elif structured.get("cookie_session_neutral") is False:
        total += 1
        noise_features.append("COOKIE_SESSION_EFFECT")

    explicit = structured.get("confidence")
    confidence = float(explicit) if isinstance(explicit, (int, float)) else (points / total if total else 0.0)
    confidence = max(0.0, min(1.0, confidence))
    strategy = "ACCEPT" if confidence >= 0.8 else "CALIBRATE" if confidence >= 0.5 else "CHANGE_PAYLOAD"
    return {
        "confidence": round(confidence, 4),
        "signal_features": sorted(set(signal_features)),
        "noise_features": sorted(set(noise_features)),
        "true_profile": true_profile,
        "false_profile": false_profile,
        "control_profile": control_profile,
        "recommended_strategy": strategy,
        "quality_fingerprint": hashlib.sha256(json.dumps(structured, sort_keys=True, default=str).encode()).hexdigest(),
    }
