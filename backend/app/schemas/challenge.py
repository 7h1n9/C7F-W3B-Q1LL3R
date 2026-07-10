from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


class ChallengeInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    target_url: str = Field(max_length=2048)
    allowed_hosts: list[str] = Field(min_length=1)
    flag_pattern: str = r"flag\{[^}]+\}"
    source_path: str | None = None
    status: str = "ACTIVE"

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, values: list[str]) -> list[str]:
        cleaned = sorted({value.lower().strip() for value in values if value.strip()})
        if not cleaned:
            raise ValueError("allowed_hosts must not be empty")
        return cleaned

    @model_validator(mode="after")
    def target_must_be_allowed(self) -> "ChallengeInput":
        parsed = urlparse(self.target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target_url must be an absolute HTTP(S) URL")
        if parsed.hostname.lower() not in self.allowed_hosts:
            raise ValueError("target URL host must be included in allowed_hosts")
        return self


class ChallengeRead(ChallengeInput):
    id: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}
