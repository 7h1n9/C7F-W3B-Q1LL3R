from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


def _extract_hostname(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate if "://" in candidate else f"//{candidate}", scheme="http")
    return (parsed.hostname or candidate).lower().strip()


def normalize_asset_warranty_metadata(challenge: "ChallengeInput") -> dict:
    """Normalize the durable metadata required by the asset-warranty adapter."""
    metadata = dict(challenge.metadata_json or {})
    adapter = str(metadata.get("adapter") or "").lower()
    text = f"{challenge.name or ''}\n{challenge.description or ''}".lower()
    looks_like_asset_warranty = (
        "资产保修" in text
        or "asset warranty" in text
        or ("asset_no" in text and "department" in text)
    )
    if adapter != "asset_warranty" and not looks_like_asset_warranty:
        return metadata
    metadata.setdefault("adapter", "asset_warranty")
    metadata.setdefault("dbms", "mysql")
    metadata.setdefault("endpoint", "/api/warranty/check")
    metadata.setdefault("method", "POST")
    metadata.setdefault("content_type", "application/json")
    metadata.setdefault("fields", ["asset_no", "department"])
    metadata.setdefault("control_values", {"asset_no": "PC-2026-013", "department": "OPS"})
    return metadata


class ChallengeInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    challenge_type: str = Field(default="WEB_TARGET", pattern="^(WEB_TARGET|TRAFFIC_ANALYSIS)$")
    target_url: str | None = Field(default=None, max_length=2048)
    allowed_hosts: list[str] = Field(default_factory=list)
    flag_pattern: str = r"flag\{[^}]+\}"
    source_path: str | None = None
    status: str = "ACTIVE"
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, values: list[str]) -> list[str]:
        expanded: list[str] = []
        for value in values:
            expanded.extend(part for part in value.replace("\n", ",").replace(";", ",").split(","))
        cleaned = sorted({_extract_hostname(value) for value in expanded if value.strip()})
        return cleaned

    @model_validator(mode="after")
    def target_must_be_allowed(self) -> "ChallengeInput":
        if self.challenge_type == "WEB_TARGET":
            parsed = urlparse(self.target_url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("target_url must be an absolute HTTP(S) URL")
            if not self.allowed_hosts or parsed.hostname.lower() not in self.allowed_hosts:
                raise ValueError("target URL host must be included in a non-empty allowed_hosts")
        elif self.target_url or self.allowed_hosts:
            raise ValueError(
                "traffic-analysis challenges must not define target_url or allowed_hosts"
            )
        self.metadata_json = normalize_asset_warranty_metadata(self)
        metadata = self.metadata_json
        if metadata.get("adapter") == "asset_warranty":
            if metadata.get("dbms", "mysql") != "mysql":
                raise ValueError("asset_warranty challenges must declare metadata_json.dbms=mysql")
            metadata.setdefault("dbms", "mysql")
            metadata.setdefault("endpoint", "/api/warranty/check")
            metadata.setdefault("method", "POST")
            metadata.setdefault("content_type", "application/json")
            metadata.setdefault("fields", ["asset_no", "department"])
            metadata.setdefault("control_values", {"asset_no": "PC-2026-013", "department": "OPS"})
            self.metadata_json = metadata
        return self


class ChallengeRead(ChallengeInput):
    id: str
    run_count: int = 0
    solved_run_count: int = 0
    latest_run_status: str | None = None
    latest_run_started_at: str | None = None
    latest_run_engine_type: str | None = None
    created_at: str
    updated_at: str
    primary_attachment_id: str | None = None

    model_config = {"from_attributes": True}
