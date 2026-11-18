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


def _declared_hints(challenge: object, *, name: str) -> dict[str, Any]:
    """Hints the challenge itself declares, when no curated set matches.

    A benchmark case ships ``initial_knowledge`` and ``attack_surface`` in its
    metadata: the endpoint, the one injectable parameter, and what the operator
    already knows about the target.  That is the case's ground truth, and it was
    being dropped on the floor -- Run ``e087202c`` walked ``/search``,
    ``/track`` and ``/lookup`` (all 404) for minutes while
    ``POST /api/warranty/check`` sat unread in the case file it was created
    from.

    Only data already stored on this Run's own Challenge is used, so a challenge
    that declares nothing keeps the old empty hint set.
    """

    empty: dict[str, Any] = {"id": "none", "challenge": name, "hints": [], "constraints": {}}
    metadata = getattr(challenge, "metadata_json", None) or {}
    if not isinstance(metadata, dict):
        return empty
    raw_knowledge = metadata.get("initial_knowledge")
    knowledge = (
        [
            str(item).strip()
            for item in raw_knowledge
            if item is not None and str(item).strip()
        ]
        if isinstance(raw_knowledge, (list, tuple))
        else []
    )
    raw_surface = metadata.get("attack_surface")
    surface = (
        [item for item in raw_surface if isinstance(item, dict)]
        if isinstance(raw_surface, (list, tuple))
        else []
    )
    hints = list(knowledge)
    inputs: list[str] = []
    for item in surface:
        endpoint = str(item.get("endpoint") or "").strip()
        if not endpoint:
            continue
        method = str(item.get("method") or "GET").strip().upper()
        parameter = str(item.get("parameter") or "").strip()
        detail = f"Declared attack surface: {method} {endpoint}"
        if parameter:
            detail += f" (parameter: {parameter})"
            if parameter not in inputs:
                inputs.append(parameter)
        hints.append(detail)
    if not hints:
        return empty
    constraints: dict[str, Any] = {
        "method": str(surface[0].get("method") or "GET").strip().upper() if surface else "GET",
        "endpoint": str(surface[0].get("endpoint") or "").strip() if surface else "",
    }
    if inputs:
        constraints["allowed_inputs"] = inputs
    identifier = str(metadata.get("benchmark_case_id") or name or "declared")
    return {
        "id": f"declared:{identifier}",
        "challenge": name,
        "hints": hints,
        "constraints": constraints,
    }


def hints_for_challenge(challenge: object) -> dict[str, Any]:
    name = str(getattr(challenge, "name", "") or "")
    target = str(getattr(challenge, "target_url", "") or "")
    if "资产保修核验平台" in name or target.endswith(":28319/") or target.endswith(":28319"):
        return {"id": ASSET_WARRANTY_HINTS["id"], "challenge": ASSET_WARRANTY_HINTS["challenge"], "hints": list(ASSET_WARRANTY_HINTS["hints"]), "constraints": dict(ASSET_WARRANTY_HINTS["constraints"])}
    declared = _declared_hints(challenge, name=name)
    if declared["hints"]:
        return declared
    return {"id": "none", "challenge": name, "hints": [], "constraints": {}}


def brief_lines(challenge: object) -> list[str]:
    """The same declarations as :func:`hint_text`, shaped for a chat prompt.

    ``hint_text`` produces a markdown section for the CLI workspace's
    ``AGENTS.md``.  An in-process Chat Completions Worker never reads that file,
    so the challenge's own declarations (declared endpoint, the one injectable
    parameter, fixed control values) reached only the CLI arm.  Run
    ``e3391674`` shows the cost: the Reason model wrote the right technique
    ("boolean/LIKE injection oracle ... tautology/contradiction pairs") but
    guessed the parameters as ``serial`` / ``warranty_id`` / ``product_id``,
    because the only names it ever saw were the ones it invented.

    Returning plain lines (no heading, no markdown) keeps the caller free to
    place them in a JSON field or prepend them to a prompt without nesting
    headers inside another section.
    """

    hints = hints_for_challenge(challenge)
    lines = [str(item).strip() for item in hints.get("hints", []) if str(item).strip()]
    constraints = hints.get("constraints") or {}
    method = str(constraints.get("method") or "").strip().upper()
    endpoint = str(constraints.get("endpoint") or "").strip()
    if method and endpoint:
        lines.append(f"Declared entry point: {method} {endpoint}")
    for key, value in (constraints.get("control_values") or {}).items():
        lines.append(f"Fixed control value: {key}={value}")
    return lines


def hint_text(hints: dict[str, Any]) -> str:
    lines = ["## Authorized Methodology Hints"]
    lines.extend(str(item) for item in hints.get("hints", []))
    values = (hints.get("constraints") or {}).get("control_values") or {}
    if values:
        lines.append("Fixed control values while testing the other field:")
        lines.extend(f"- {key}: {value}" for key, value in values.items())
    return "\n".join(lines) + "\n"
