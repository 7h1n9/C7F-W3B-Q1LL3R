import re
from typing import ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.net import ensure_absolute_http_url

DEFAULT_FLAG_PATTERN = r"flag\{[^}]+\}"
# A bare flag "banner" -- ``CTF2``, ``flag``, ``DASCTF`` -- is what operators
# actually know; the ``{...}`` body is boilerplate.  Anything already containing
# regex syntax is treated as the operator's own expression and left untouched,
# so existing challenges keep working.
_FLAG_BODY = r"\{[^}]+\}"
_REGEX_META = frozenset("\\^$.|?*+()[]{}")


def normalize_flag_pattern(value: str | None) -> str:
    """Expand a bare flag banner into the full acceptance regex.

    ``"CTF2"`` becomes ``CTF2\\{[^}]+\\}``.  A value that already looks like a
    regular expression (``CTF2\\{[^}]+\\}``, ``(?i)flag\\{[^}]+\\}``) is returned
    unchanged.
    """

    text = str(value or "").strip()
    if not text:
        return DEFAULT_FLAG_PATTERN
    if any(char in _REGEX_META for char in text):
        return text
    head = text.strip("{}").strip()
    if not head:
        return DEFAULT_FLAG_PATTERN
    return f"{re.escape(head)}{_FLAG_BODY}"


def flag_head_of(pattern: str | None) -> str:
    """Return the bare banner of a canonical pattern, else the value itself."""

    text = str(pattern or "").strip()
    if not text or not text.endswith(_FLAG_BODY):
        return text
    head = text[: -len(_FLAG_BODY)]
    if head and not any(char in _REGEX_META for char in head):
        return head
    return text


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
    flag_pattern: str = DEFAULT_FLAG_PATTERN
    source_path: str | None = None
    status: str = "ACTIVE"
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("flag_pattern", mode="before")
    @classmethod
    def expand_flag_banner(cls, value: str | None) -> str:
        """Accept a bare banner such as ``CTF2`` and store the full regex."""

        return normalize_flag_pattern(value)


    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, values: list[str]) -> list[str]:
        expanded: list[str] = []
        for value in values:
            expanded.extend(part for part in value.replace("\n", ",").replace(";", ",").split(","))
        cleaned = sorted({_extract_hostname(value) for value in expanded if value.strip()})
        return cleaned

    #: Whether this model enforces the *input* target contract.  It is an
    #: operator-facing rule for creating and editing a challenge, not a property
    #: of stored data, so the read schema clears it: a list endpoint that 500s
    #: because one legacy row has a bare ``host:port`` or no target at all is
    #: strictly worse than the row it failed on.
    _enforce_target_contract: ClassVar[bool] = True

    @model_validator(mode="after")
    def target_must_be_allowed(self) -> "ChallengeInput":
        if self._enforce_target_contract:
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

    #: A read must render stored state.  Enforcing the input contract here made
    #: one legacy row -- a bare ``host:port`` from before the arena solve path
    #: normalised addresses, or a ``[无flag]`` teaching challenge with no target
    #: at all -- fail the entire challenge list with HTTP 500.
    _enforce_target_contract: ClassVar[bool] = False

    @field_validator("target_url", mode="before")
    @classmethod
    def tolerate_a_legacy_scheme_less_address(cls, value: object) -> object:
        """Normalise a stored bare ``host:port`` instead of rejecting it."""

        return ensure_absolute_http_url(value if isinstance(value, str) else None)
