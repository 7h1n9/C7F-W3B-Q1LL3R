"""Deterministic attack-chain planning for evidence-driven web solving."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AttackChainNode:
    node_id: str
    title: str
    objective: str
    required_capabilities: tuple[str, ...]
    tool_hints: tuple[str, ...]
    priority: int
    status: str = "LOCKED"


class AttackChainPlan:
    """A serializable dependency graph; no secrets or token values are stored."""

    @staticmethod
    def easy_jwt() -> dict:
        nodes = [
            AttackChainNode("N1", "建立基线", "确认目标响应、入口和技术面", (), ("http_request",), 100, "READY"),
            AttackChainNode("N2", "确认认证入口", "确认 login/register 和会话承载方式", ("can_read_public_page",), ("http_request",), 95),
            AttackChainNode("N3", "建立会话", "通过注册或登录获得隔离 HTTP 会话", ("can_read_auth_entry",), ("http_session_request",), 90),
            AttackChainNode("N4", "提取令牌句柄", "从会话响应中建立 JWT Secret Handle", ("can_reuse_session",), ("http_session_inspect",), 88),
            AttackChainNode("N5", "解析 JWT", "只读取 JWT header/claims 元数据", ("has_jwt_handle",), ("jwt_inspect",), 85),
            AttackChainNode("N6", "确认签名路径", "验证算法、claims 变化和签名接口可用性", ("can_forge_token",), ("jwt_clone_claims", "jwt_sign"), 80),
            AttackChainNode("N7", "注入认证会话", "把签名结果作为 Cookie Handle 写入会话", ("has_signed_jwt",), ("http_session_set_cookie_ref",), 75),
            AttackChainNode("N8", "访问受保护资源", "使用认证会话访问 admin/flag 路径", ("can_set_auth_cookie",), ("http_session_request",), 70),
            AttackChainNode("N9", "提取候选 Flag", "从受保护响应建立候选并保存证据", ("can_read_protected_page",), ("http_extract",), 60),
            AttackChainNode("N10", "验证 Flag", "使用独立验证器确认动态 Flag", ("has_flag_candidate",), (), 50),
        ]
        return {
            "chain_id": "easy-jwt-auth-forge-v1",
            "current_node_id": "N1",
            "nodes": [node.__dict__ for node in nodes],
            "completed_node_ids": [],
            "blocked_node_ids": [],
            "last_transition_reason": "initialized",
        }

    @staticmethod
    def generic() -> dict:
        return {
            "chain_id": "generic-evidence-loop-v1",
            "current_node_id": "N1",
            "nodes": [
                {"node_id": "N1", "title": "建立基线", "objective": "确认目标和授权边界", "required_capabilities": [], "tool_hints": ["http_request", "file_read"], "priority": 100, "status": "READY"},
                {"node_id": "N2", "title": "提出假设", "objective": "提出可证伪的攻击假设", "required_capabilities": ["can_read_public_page"], "tool_hints": ["http_request", "file_search"], "priority": 80, "status": "LOCKED"},
                {"node_id": "N3", "title": "验证并提取", "objective": "验证候选并提取 Flag", "required_capabilities": ["has_tested_hypothesis"], "tool_hints": ["http_extract"], "priority": 60, "status": "LOCKED"},
            ],
            "completed_node_ids": [],
            "blocked_node_ids": [],
            "last_transition_reason": "initialized",
        }


def build_attack_chain(challenge_name: str = "", description: str = "") -> dict:
    text = f"{challenge_name} {description}".lower()
    if any(term in text for term in ("jwt", "json web token", "token forge", "令牌")):
        return AttackChainPlan.easy_jwt()
    return AttackChainPlan.generic()


def reduce_capability(ledger: dict, plan: dict, capability: str, evidence: dict | None = None) -> tuple[dict, dict, dict | None]:
    """Update ledger and deterministically unlock the next satisfied node."""
    ledger = dict(ledger or {})
    if capability:
        ledger[capability] = {
            "confirmed": True,
            "evidence": evidence or {},
            "confirmed_at": ledger.get(capability, {}).get("confirmed_at"),
        }
    plan = dict(plan or {})
    nodes = [dict(item) for item in plan.get("nodes", [])]
    completed = set(plan.get("completed_node_ids", []))
    current = plan.get("current_node_id")
    for node in nodes:
        required = set(node.get("required_capabilities") or [])
        satisfied = required.issubset(ledger)
        if node["node_id"] == current and satisfied and node["node_id"] not in completed:
            completed.add(node["node_id"])
        if node["node_id"] in completed:
            node["status"] = "COMPLETED"
        elif satisfied:
            node["status"] = "READY"
        else:
            node["status"] = "LOCKED"
    ready = [node for node in nodes if node["status"] == "READY" and node["node_id"] not in completed]
    if ready:
        selected = sorted(ready, key=lambda item: (-int(item.get("priority", 0)), item["node_id"]))[0]
        if selected["node_id"] != current:
            plan["current_node_id"] = selected["node_id"]
            plan["last_transition_reason"] = f"capability.confirmed:{capability}"
            current = selected["node_id"]
    plan["nodes"] = nodes
    plan["completed_node_ids"] = sorted(completed)
    return ledger, plan, next((node for node in nodes if node["node_id"] == current), None)


def classify_rejection(code: str | None) -> str:
    code = str(code or "").upper()
    if code in {"DUPLICATE_ACTION", "TOOL_INVALID_ARGUMENT", "TOOL_NOT_AVAILABLE", "SKILL_DECISION_REQUIRED", "SKILL_NOT_FOUND", "RUN_TOOL_NOT_ALLOWED", "TOOL_NOT_ENABLED", "SCHEMA_VALIDATION_FAILED", "AGENT_ACTION_PARSE_FAILED"}:
        return "CONTROL_REJECTION"
    if code in {"TARGET_UNREACHABLE", "REQUIRED_ATTACHMENT_MISSING", "ATTACHMENT_MISSING", "PROVIDER_CONFIGURATION_INVALID", "RUNNER_CONFIGURATION_INVALID", "RUNNER_UNAVAILABLE", "AUTHORIZATION_BOUNDARY_UNCLEAR", "REQUIRED_USER_SECRET_MISSING"}:
        return "BLOCKED"
    if code in {"HTTP_401", "HTTP_403", "HTTP_404", "JWT_INVALID", "NO_MATCH"}:
        return "NEGATIVE"
    return "ERROR"
