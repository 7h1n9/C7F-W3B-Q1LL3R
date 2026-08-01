"""Metadata-only adapter for the authorized asset-warranty challenge.

The adapter describes stage contracts and request constraints.  It never
contains a flag, source table, source column, or answer value.  All target
specific values are read from ``Challenge.metadata_json`` so the controller
does not grow another hard-coded challenge path.
"""

from __future__ import annotations

from typing import Any

from app.models.challenge import Challenge


class AssetWarrantyAdapter:
    key = "asset_warranty"

    @staticmethod
    def matches(challenge: Challenge) -> bool:
        metadata = challenge.metadata_json or {}
        return (
            str(metadata.get("adapter") or "").lower() == AssetWarrantyAdapter.key
            and str(metadata.get("dbms") or "").lower() == "mysql"
        )

    @staticmethod
    def context(challenge: Challenge) -> dict[str, Any]:
        metadata = challenge.metadata_json or {}
        endpoint = metadata.get("endpoint")
        method = metadata.get("method")
        content_type = metadata.get("content_type")
        fields = metadata.get("fields")
        controls = metadata.get("control_values")
        request = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
        endpoint = endpoint or request.get("endpoint") or request.get("path")
        method = method or request.get("method")
        content_type = content_type or request.get("content_type")
        fields = fields if isinstance(fields, list) else request.get("fields")
        controls = controls if isinstance(controls, dict) else request.get("control_values")
        result: dict[str, Any] = {
            "adapter": AssetWarrantyAdapter.key,
            "target_url": challenge.target_url,
            "allowed_hosts": list(challenge.allowed_hosts or []),
            "stages": [
                "surface_recon",
                "business_baseline",
                "boolean_oracle",
                "mysql_metadata",
                "bounded_extraction",
                "flag_verification",
            ],
            "constraints": {
                "injection_forbidden_during_recon": True,
                "fresh_verification_required": True,
            },
        }
        if endpoint:
            result["endpoint"] = str(endpoint)
        if method:
            result["method"] = str(method).upper()
        if content_type:
            result["content_type"] = str(content_type)
        if isinstance(fields, list):
            result["fields"] = [str(item) for item in fields]
        if isinstance(controls, dict):
            result["control_values"] = dict(controls)
        return result


def adapter_for(challenge: Challenge) -> AssetWarrantyAdapter | None:
    return AssetWarrantyAdapter() if AssetWarrantyAdapter.matches(challenge) else None
