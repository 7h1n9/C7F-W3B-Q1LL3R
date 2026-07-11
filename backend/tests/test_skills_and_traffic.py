from pathlib import Path

import pytest

from app.schemas.challenge import ChallengeInput
from app.schemas.skill import SkillWrite
from app.services.builtin_skills import BuiltinSkillSyncService
from app.services.skill_selection import allowed_tools_for


def test_traffic_challenge_rejects_network_target() -> None:
    with pytest.raises(ValueError):
        ChallengeInput(name="traffic", challenge_type="TRAFFIC_ANALYSIS", target_url="http://target.local", allowed_hosts=[])


def test_traffic_challenge_allows_no_target_before_attachment_upload() -> None:
    challenge = ChallengeInput(name="traffic", challenge_type="TRAFFIC_ANALYSIS", target_url=None, allowed_hosts=[])
    assert challenge.challenge_type == "TRAFFIC_ANALYSIS"


def test_skill_validation_rejects_shell_commands() -> None:
    with pytest.raises(ValueError):
        SkillWrite(name="unsafe", display_name="Unsafe", challenge_types=["WEB_TARGET"], content_markdown="$ curl http://outside.example")


def test_builtin_skill_front_matter_is_parseable() -> None:
    root = Path(__file__).resolve().parents[2] / "configs" / "skills"
    metadata, content = BuiltinSkillSyncService(root)._parse(root / "traffic-analysis-basic" / "SKILL.md")
    assert metadata["name"] == "traffic-analysis-basic"
    assert "PCAP" in content


def test_traffic_tools_exclude_http_request() -> None:
    assert "http_request" not in allowed_tools_for("TRAFFIC_ANALYSIS")
    assert {"pcap_metadata", "pcap_protocols", "pcap_query"}.issubset(allowed_tools_for("TRAFFIC_ANALYSIS"))
