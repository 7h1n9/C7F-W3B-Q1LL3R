"""Evidence-driven dynamic attack-chain primitives."""
from dataclasses import dataclass, asdict

@dataclass
class AttackChainNode:
    node_id: str
    title: str
    objective: str
    status: str = "LOCKED"
    prerequisites: list[str] | None = None
    supporting_evidence_ids: list[str] | None = None
    preferred_tools: list[str] | None = None
    candidate_payloads: list[dict] | None = None
    success_signals: list[str] | None = None
    negative_signals: list[str] | None = None
    max_attempts: int = 3
    attempt_count: int = 0
    failure_pivot: str = "换用下一层假设并保留差异证据"
    priority: int = 0

    def __post_init__(self):
        for field in ("prerequisites", "supporting_evidence_ids", "preferred_tools", "candidate_payloads", "success_signals", "negative_signals"):
            if getattr(self, field) is None:
                setattr(self, field, [])

def initial_chain() -> dict:
    nodes = [
        AttackChainNode("N1", "题目与授权确认", "确认目标、授权边界和公开入口", "READY", preferred_tools=["http_request"], priority=100),
        AttackChainNode("N2", "建立请求基线", "记录正常响应及可比较字段", prerequisites=["can_read_public_page"], preferred_tools=["http_request", "http_compare"], priority=95),
        AttackChainNode("N3", "技术栈与入口识别", "从响应、源码和资源定位可验证入口", prerequisites=["baseline_confirmed"], preferred_tools=["http_extract", "js_asset_analyze"], priority=90),
        AttackChainNode("N4", "攻击面分类", "判断文件、Cookie、WebSocket、参数或框架证据", prerequisites=["entry_identified"], preferred_tools=["http_compare", "signed_cookie_detect", "websocket_handshake"], priority=85),
        AttackChainNode("N5", "选择最高价值假设", "以证据和 Flag 距离排序实验", prerequisites=["attack_surface_classified"], preferred_tools=["http_compare"], priority=80),
    ]
    return {"chain_id": "dynamic-evidence-v2", "current_node_id": "N1", "nodes": [asdict(n) for n in nodes], "completed_node_ids": [], "blocked_node_ids": [], "last_transition_reason": "initialized"}

def build_attack_chain(challenge_name: str = "", description: str = "") -> dict:
    return initial_chain()

def reduce_capability(ledger: dict, plan: dict, capability: str, evidence: dict | None = None):
    ledger = dict(ledger or {})
    if capability:
        ledger[capability] = {"confirmed": True, "evidence": evidence or {}}
    plan = dict(plan or initial_chain())
    completed = set(plan.get("completed_node_ids", []))
    nodes = [dict(n) for n in plan.get("nodes", [])]
    for node in nodes:
        if node["node_id"] in completed:
            node["status"] = "CONFIRMED"; continue
        if all(item in ledger for item in node.get("prerequisites", [])):
            node["status"] = "READY"
    current = next((n for n in nodes if n["node_id"] == plan.get("current_node_id")), None)
    if current and current["status"] == "READY" and current.get("prerequisites") and all(x in ledger for x in current["prerequisites"]):
        completed.add(current["node_id"])
    ready = [n for n in nodes if n["status"] == "READY" and n["node_id"] not in completed]
    if ready:
        selected = max(ready, key=lambda n: (n.get("priority", 0), n["node_id"]))
        plan["current_node_id"] = selected["node_id"]
    plan["completed_node_ids"] = sorted(completed)
    plan["nodes"] = nodes
    plan["last_transition_reason"] = f"capability.confirmed:{capability}"
    return ledger, plan, next((n for n in nodes if n["node_id"] == plan["current_node_id"]), None)

def classify_rejection(code: str | None) -> str:
    code = str(code or "").upper()
    if code in {"TOOL_INVALID_ARGUMENT", "SCHEMA_VALIDATION_FAILED", "DUPLICATE_ACTION"}: return "CONTROL_REJECTION"
    if code in {"CTFCTL_LEASE_INVALID", "TARGET_UNREACHABLE", "REQUIRED_USER_SECRET_MISSING"}: return "BLOCKED"
    if code.startswith(("HTTP_", "JWT_")) or code in {"NO_MATCH", "FILE_NOT_FOUND"}: return "NEGATIVE"
    return "ERROR"
