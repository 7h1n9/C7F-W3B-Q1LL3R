"""Durable, challenge-scoped methodology hints."""

from __future__ import annotations

from typing import Any

ASSET_WARRANTY_HINTS: dict[str, Any] = {
    "id": "asset-warranty-h1-h3",
    "challenge": "资产保修核验平台",
    "hints": [
        "H1: 先建立记录存在与记录不存在两种稳定响应，再分别检查两个输入字段。",
        "H2: 查询结果不展示具体数据，但真假响应足以验证附加条件是否成立。",
        "H3: 其中一个字段由旧版查询逻辑直接参与条件拼接，可通过布尔结果逐步推断数据库中的配置值。",
    ],
    "constraints": {
        "allowed_inputs": ["asset_no", "department"],
        "control_values": {"asset_no": "PC-2026-013", "department": "OPS"},
        "endpoint": "/api/warranty/check",
        "method": "POST",
        "content_type": "application/json",
    },
}


def hints_for_challenge(challenge: object) -> dict[str, Any]:
    name = str(getattr(challenge, "name", "") or "")
    target = str(getattr(challenge, "target_url", "") or "")
    if "资产保修核验平台" in name or target.endswith(":28319/") or target.endswith(":28319"):
        return {"id": ASSET_WARRANTY_HINTS["id"], "challenge": ASSET_WARRANTY_HINTS["challenge"], "hints": list(ASSET_WARRANTY_HINTS["hints"]), "constraints": dict(ASSET_WARRANTY_HINTS["constraints"])}
    return {"id": "none", "challenge": name, "hints": [], "constraints": {}}


def hint_text(hints: dict[str, Any]) -> str:
    lines = ["## Authorized Methodology Hints"]
    lines.extend(str(item) for item in hints.get("hints", []))
    values = (hints.get("constraints") or {}).get("control_values") or {}
    if values:
        lines.append("Fixed control values while testing the other field:")
        lines.extend(f"- {key}: {value}" for key, value in values.items())
    return "\n".join(lines) + "\n"
