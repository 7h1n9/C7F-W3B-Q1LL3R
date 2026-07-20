"""Deterministic quality gate for Chinese CTF writeups."""
REQUIRED = ("一句话解法", "攻击链", "核心突破点", "漏洞根因", "关键接口", "关键参数", "Payload", "手动复现", "Flag 来源", "修复建议")

def validate_writeup(text: str, payload_required: bool = True) -> dict:
    checks = {key: key in text for key in REQUIRED}
    if not payload_required:
        checks["Payload"] = "payload_not_required_reason" in text
    score = round(sum(checks.values()) / len(checks) * 100)
    return {"passed": score >= 85, "score": score, "checks": checks, "missing": [k for k, value in checks.items() if not value]}
