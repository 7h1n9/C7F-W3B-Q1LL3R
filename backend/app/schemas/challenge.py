from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


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
        cleaned = sorted({value.lower().strip() for value in values if value.strip()})
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
        return self


class ChallengeRead(ChallengeInput):
    id: str
    created_at: str
    updated_at: str
    primary_attachment_id: str | None = None

    model_config = {"from_attributes": True}
