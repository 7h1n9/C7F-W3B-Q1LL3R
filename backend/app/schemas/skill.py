import re

from pydantic import BaseModel, Field, field_validator

KNOWN_TOOLS = {
    "http_request",
    "file_read",
    "file_search",
    "python_run",
    "pcap_metadata",
    "pcap_protocols",
    "pcap_query",
}
CHALLENGE_TYPES = {"WEB_TARGET", "TRAFFIC_ANALYSIS"}


class SkillWrite(BaseModel):
    name: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    challenge_types: list[str] = Field(default_factory=lambda: ["WEB_TARGET"])
    content_markdown: str = Field(min_length=1, max_length=24000)
    allowed_tools: list[str] = Field(default_factory=list)
    risk_level: str = Field(default="low", pattern="^(low|medium|high)$")
    enabled: bool = True

    @field_validator("challenge_types")
    @classmethod
    def validate_types(cls, value: list[str]) -> list[str]:
        clean = sorted(set(value))
        if not clean or not set(clean).issubset(CHALLENGE_TYPES):
            raise ValueError("challenge_types contains an unsupported value")
        return clean

    @field_validator("allowed_tools")
    @classmethod
    def validate_tools(cls, value: list[str]) -> list[str]:
        clean = sorted(set(value))
        if not set(clean).issubset(KNOWN_TOOLS):
            raise ValueError("allowed_tools contains an unsupported tool")
        return clean

    @field_validator("content_markdown")
    @classmethod
    def reject_embedded_shell(cls, value: str) -> str:
        if re.search(
            r"^\s*(?:\$ |sudo\s|curl\s|wget\s|nmap\s|tshark\s|capinfos\s)",
            value,
            re.MULTILINE | re.IGNORECASE,
        ):
            raise ValueError(
                "Skill content must describe a workflow, not include executable shell commands"
            )
        return value


class SkillBindingWrite(BaseModel):
    skill_id: str
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10000)
    config_json: dict = Field(default_factory=dict)


class ChallengeSkillBindingWrite(BaseModel):
    skill_id: str
    priority: int = Field(default=100, ge=0, le=10000)
