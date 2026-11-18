"""Turn a finished Run into one auditable "what to research next" direction.

Two populations end up looking alike in the database and must never be mixed:

* the target resisted the solver (``unsolved_solver``) -- there is a research
  question worth handing to an operator, and the Run belongs in the solve-rate
  denominator;
* the harness itself failed (``aborted_infra``) -- the balance ran out, the
  provider refused, the proxy died, the target was 502/503, or an operator
  stopped the Run before any command ran.  There is nothing to research and the
  Run must be excluded from the denominator.

Everything here is a pure function of its arguments: no session, no IO, no
clock, no model call.  The caller owns persistence and wiring, so the ranking
of directions below is reproducible and testable without fixtures.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: Error codes that mean *the harness* failed, not the target.  Compared after
#: ``strip().upper()`` and by exact membership -- never by substring, because
#: ``"SOLVED" in status`` once matched ``COMPLETED_UNSOLVED``.
INFRA_ERROR_CODES: frozenset[str] = frozenset({
    "INSUFFICIENT_BALANCE",
    "INSUFFICIENT_QUOTA",
    "INSUFFICIENT_CREDITS",
    "MODEL_QUOTA_EXCEEDED",
    "MODEL_RATE_LIMITED",
    "MODEL_BALANCE_EXHAUSTED",
    "PROVIDER_DENIED",
    "OPENAI_COMPATIBLE_PROVIDER_DENIED",
    "TARGET_UNAVAILABLE",
    "BACKEND_UNAVAILABLE",
    "RUNNER_UNAVAILABLE",
    "TARGET_UNREACHABLE",
    "PROXY_CONNECT_ERROR",
    "UPSTREAM_UNAVAILABLE",
    "HTTP_402",
    "HTTP_429",
    "HTTP_502",
    "HTTP_503",
})

#: Closed taxonomy used for coverage gaps.  Sorted so the tuple is stable and a
#: caller can diff two runs' coverage without a set ordering surprise.
VULN_CLASSES: tuple[str, ...] = (
    "auth_bypass",
    "command_injection",
    "deserialization",
    "file_include",
    "file_upload",
    "idor",
    "information_disclosure",
    "path_traversal",
    "race_condition",
    "sql_injection",
    "ssrf",
    "ssti",
    "weak_crypto",
    "xss",
    "xxe",
)

#: Exact statuses that mean a flag was captured.  ``COMPLETED_UNSOLVED`` is
#: deliberately absent: it shares the ``COMPLETED_`` prefix and nothing else.
SOLVED_STATUSES: frozenset[str] = frozenset({"COMPLETED_SOLVED", "SOLVED"})

_ABORTED_STATUSES: frozenset[str] = frozenset({"CANCELLED"})

# Diagnostic messages are short.  Tool transcripts, exploit payloads and model
# output are long and may quote arbitrary tokens ("timeout", "connection
# refused") as *data*; matching those would launder a solver failure into an
# infrastructure abort and silently shrink the denominator.  We therefore only
# read the first 400 characters of a message, and only against anchored
# diagnostic phrases.
_MESSAGE_WINDOW = 400

_INFRA_MESSAGE_PATTERN = re.compile(
    "|".join(
        (
            # Balance/quota, English, snake_case and Chinese.
            r"\binsufficient[ _]?(?:balance|quota|credits?)\b",
            r"\b(?:balance|quota|credits?)[ _]?(?:is[ _]?)?(?:exhausted|不足|exceeded)\b",
            r"余额不足|配额不足|额度不足|账户余额",
            # Provider refusal.
            r"\bprovider[ _]denied\b",
            r"\bprovider[ _](?:refused|declined)\b",
            r"提供商拒绝|模型拒绝服务",
            # 402 / 429 in a diagnostic frame.
            r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[ _]code)?|error|code)\s*[:=]?\s*(?:402|429)\b",
            r"\b402\s*payment[ _]required\b",
            r"\b429\s*too[ _]many[ _]requests\b",
            # Proxy / network / TLS.
            r"\bconnection[ _]refused\b",
            r"\beconn(?:refused|reset)\b",
            r"\bproxy[ _]connect(?:ion)?[ _](?:error|failed|refused)\b",
            r"\bssl[ _](?:handshake[ _]failure|handshake|certificate[ _]verify[ _]failed)\b",
            r"\b(?:proxy|upstream)\b[^.\n]{0,60}\b(?:timed?[ _]?out|timeout)\b",
            r"\b(?:timed?[ _]?out|timeout)\b[^.\n]{0,60}\b(?:proxy|upstream)\b",
            r"代理[^。\n]{0,20}(?:超时|不可达|连接失败)",
            # Target / control-plane outage, only as the canonical phrase.
            r"\b50[23]\s+(?:service[ _]unavailable|bad[ _]gateway)\b",
            r"\b504\s+gateway[ _]time[ _]?out\b",
            r"\btarget[ _](?:unavailable|unreachable)\b",
            r"\bbackend[ _]unavailable\b",
            r"\brunner[ _](?:unavailable|not[ _]reachable)\b",
            r"目标(?:不可达|不可用)|后端不可用|执行器不可用",
        )
    ),
    re.IGNORECASE,
)

# Keyword table for :func:`classify_text`.  ASCII terms are matched with word
# boundaries (``xss`` must not fire inside ``fizzss``), Chinese terms as plain
# substrings because Unicode word boundaries are unreliable for them.
_VULN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sql_injection": (
        "sql injection", "sqli", "sql注入", "sql 注入", "union select", "报错注入",
        "布尔盲注", "时间盲注", "盲注", "堆叠注入", "宽字节注入",
    ),
    "xss": (
        "xss", "cross-site scripting", "cross site scripting", "跨站脚本",
        "<script", "反射型", "存储型", "dom型",
    ),
    "ssrf": (
        "ssrf", "server-side request forgery", "server side request forgery",
        "服务端请求伪造", "内网探测", "云元数据",
    ),
    "ssti": (
        "ssti", "template injection", "模板注入", "模板表达式", "jinja2",
        "twig", "freemarker", "velocity",
    ),
    "deserialization": (
        "deserialization", "deserialize", "反序列化", "ysoserial", "fastjson",
        "shiro", "pickle", "readobject",
    ),
    "file_upload": (
        "file upload", "文件上传", "上传绕过", "multipart", "webshell",
    ),
    "file_include": (
        "file inclusion", "文件包含", "lfi", "rfi", "php://filter",
        "php://input", "本地包含", "远程包含",
    ),
    "path_traversal": (
        "path traversal", "directory traversal", "路径穿越", "目录遍历",
        "目录穿越", "..%2f", "%2e%2e",
    ),
    "command_injection": (
        "command injection", "os command", "命令注入", "命令执行", "代码执行",
        "rce", "remote code execution", "code execution", "系统命令",
    ),
    "auth_bypass": (
        "auth bypass", "authentication bypass", "authorization bypass",
        "认证绕过", "登录绕过", "默认口令", "默认密码", "弱口令", "弱密码",
    ),
    "idor": (
        "idor", "insecure direct object", "越权", "水平越权", "垂直越权",
        "对象引用",
    ),
    "xxe": (
        "xxe", "xml external entity", "外部实体", "实体注入",
    ),
    "race_condition": (
        "race condition", "竞态", "条件竞争", "并发竞争", "race window",
    ),
    "weak_crypto": (
        "weak crypto", "weak encryption", "弱加密", "弱哈希", "弱随机",
        "padding oracle", "jwt none", "硬编码密钥", "md5",
    ),
    "information_disclosure": (
        "information disclosure", "info leak", "信息泄露", "信息泄漏",
        "敏感信息", "源码泄露", "源码泄漏", "备份文件", "debug 信息",
        ".git/",
    ),
}


def _compile_vuln_patterns() -> tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...]:
    compiled: list[tuple[str, re.Pattern[str], tuple[str, ...]]] = []
    for vuln_class in VULN_CLASSES:
        keywords = _VULN_KEYWORDS[vuln_class]
        ascii_keywords = tuple(k for k in keywords if k.isascii())
        raw_keywords = tuple(k for k in keywords if not k.isascii())
        pattern = re.compile(
            r"(?<![a-z0-9_])" + "|".join(re.escape(k) for k in ascii_keywords)
            + r"(?![a-z0-9_])"
        ) if ascii_keywords else re.compile(r"(?!)")
        compiled.append((vuln_class, pattern, raw_keywords))
    return tuple(compiled)


_VULN_PATTERNS = _compile_vuln_patterns()


@dataclass(frozen=True)
class Outcome:
    outcome_class: str
    infra_reasons: tuple[str, ...]
    excluded_from_solve_rate: bool


@dataclass(frozen=True)
class NextDirection:
    action: str
    rationale: str
    evidence: tuple[str, ...]
    confidence: str
    source: str


_INFRA_REASON_BY_CODE: dict[str, str] = {
    "INSUFFICIENT_BALANCE": "模型账户余额不足，上游拒绝继续生成（harness 侧故障）",
    "INSUFFICIENT_QUOTA": "模型配额耗尽，上游拒绝继续生成（harness 侧故障）",
    "INSUFFICIENT_CREDITS": "模型额度耗尽，上游拒绝继续生成（harness 侧故障）",
    "MODEL_QUOTA_EXCEEDED": "模型配额超限，上游拒绝继续生成（harness 侧故障）",
    "MODEL_RATE_LIMITED": "模型接口被上游限流（HTTP 429）",
    "MODEL_BALANCE_EXHAUSTED": "模型账户余额耗尽（harness 侧故障）",
    "PROVIDER_DENIED": "模型提供商拒绝请求（PROVIDER_DENIED）",
    "OPENAI_COMPATIBLE_PROVIDER_DENIED": "模型提供商拒绝请求（OPENAI_COMPATIBLE_PROVIDER_DENIED）",
    "TARGET_UNAVAILABLE": "靶标环境不可用（TARGET_UNAVAILABLE）",
    "BACKEND_UNAVAILABLE": "执行后端不可用（BACKEND_UNAVAILABLE）",
    "RUNNER_UNAVAILABLE": "Kali Runner 不可用（RUNNER_UNAVAILABLE）",
    "TARGET_UNREACHABLE": "靶标网络不可达（TARGET_UNREACHABLE）",
    "PROXY_CONNECT_ERROR": "代理连接失败（PROXY_CONNECT_ERROR）",
    "UPSTREAM_UNAVAILABLE": "上游服务不可用（UPSTREAM_UNAVAILABLE）",
    "HTTP_402": "上游返回 HTTP 402（余额或配额不足）",
    "HTTP_429": "上游返回 HTTP 429（限流）",
    "HTTP_502": "上游返回 HTTP 502（Bad Gateway）",
    "HTTP_503": "上游返回 HTTP 503（Service Unavailable）",
}


def _normalize_code(code: str | None) -> str:
    return str(code or "").strip().upper()


def _normalize_status(status: str) -> str:
    return str(status or "").strip().upper()


def _infra_reasons(
    *,
    status: str,
    error_code: str | None,
    error_message: str | None,
    command_count: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    code = _normalize_code(error_code)
    if code in INFRA_ERROR_CODES:
        reasons.append(_INFRA_REASON_BY_CODE[code])

    message = str(error_message or "")
    if message and len(message) <= _MESSAGE_WINDOW and _INFRA_MESSAGE_PATTERN.search(message):
        # One generic reason: the phrase that fired is already visible to the
        # operator in the persisted error message.
        reasons.append("错误信息命中基础设施故障特征（配额/限流/代理/上游 5xx/目标不可达）")

    if _normalize_status(status) in _ABORTED_STATUSES and command_count <= 0:
        reasons.append("运行被手动终止，且未执行任何命令（Harness 主动中断，不是靶标拒绝）")

    return tuple(dict.fromkeys(reasons))


def classify_outcome(
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    has_flag: bool = False,
    command_count: int = 0,
) -> Outcome:
    """Separate "the target held" from "the harness broke"."""

    normalized_status = _normalize_status(status)
    if has_flag or normalized_status in SOLVED_STATUSES:
        return Outcome("solved", (), False)

    reasons = _infra_reasons(
        status=normalized_status,
        error_code=error_code,
        error_message=error_message,
        command_count=command_count,
    )
    if reasons:
        return Outcome("aborted_infra", reasons, True)

    return Outcome("unsolved_solver", (), False)


def classify_text(text: str) -> frozenset[str]:
    """Vulnerability classes mentioned in free text, Chinese or English."""

    normalized = str(text or "").lower()
    if not normalized.strip():
        return frozenset()
    hits: list[str] = []
    for vuln_class, pattern, raw_keywords in _VULN_PATTERNS:
        if pattern.search(normalized) or any(keyword in normalized for keyword in raw_keywords):
            hits.append(vuln_class)
    return frozenset(hits)


def untried_vuln_classes(
    attempted_texts: Sequence[str],
    candidate_classes: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Taxonomy classes never mentioned by any attempted-topic text."""

    attempted: set[str] = set()
    for text in attempted_texts:
        attempted |= classify_text(text)
    candidates = tuple(candidate_classes) if candidate_classes is not None else VULN_CLASSES
    return tuple(sorted(set(candidates) - attempted))


def _bounded_evidence(items: Sequence[object]) -> tuple[str, ...]:
    bounded: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        bounded.append(text if len(text) <= 300 else text[:300])
        if len(bounded) >= 5:
            break
    return tuple(bounded)


def compose_next_direction(
    *,
    outcome: Outcome,
    dead_ends: Sequence[str] = (),
    verified_facts: Sequence[str] = (),
    untried_classes: Sequence[str] = (),
    open_flag_candidates: Sequence[str] = (),
    total_facts: int = 0,
    command_count: int = 0,
) -> NextDirection | None:
    """First matching rule wins; the order encodes proximity to a flag."""

    # 1. A solved Run needs no research; an infra abort has nothing to research.
    if outcome.outcome_class in {"solved", "aborted_infra"}:
        return None

    flag_evidence = _bounded_evidence(open_flag_candidates)
    if flag_evidence:
        return NextDirection(
            action="复核候选 flag 并按验证方式逐条排除",
            rationale=(
                "运行里已有疑似 flag 的候选串，离题解只差一次验证；"
                "先确认或排除这些候选，比换攻击面更接近得分。"
            ),
            evidence=flag_evidence,
            confidence="high",
            source="open_flag_candidate",
        )

    fact_evidence = _bounded_evidence(verified_facts)
    if fact_evidence and command_count <= 3:
        return NextDirection(
            action="把已验证事实转成一次利用尝试",
            rationale=(
                "运行已经证明了可复用的前提，却几乎没有再执行命令，"
                "说明事实到手后没有继续推进；沿这条事实做一次利用尝试成本最低。"
            ),
            evidence=fact_evidence,
            confidence="high",
            source="verified_foothold_unused",
        )

    # A dead end is concrete evidence about *this* target; an untried class is
    # only a generic coverage gap.  Concrete evidence wins, otherwise every
    # unsolved run would report the same "try deserialization" advice.
    recent_dead_ends = _bounded_evidence(tuple(dead_ends)[-3:])
    if recent_dead_ends:
        latest = str(tuple(dead_ends)[-1] or "").strip()
        if len(latest) > 80:
            latest = latest[:80] + "…"
        return NextDirection(
            action=(
                f"先确认最近死路「{latest}」中否掉它的判据是否真的成立，"
                "再用一个不同的判据重打一类载荷"
            ),
            rationale=(
                f"以下判断只来自最近 {len(recent_dead_ends)} 条死路记录，是待验证的假设而不是事实："
                "死路结论往往建立在回显、时序或权限判据上，判据本身可能不成立；"
                "换一个判据重打，才能区分“路走不通”和“判断错了”。"
            ),
            evidence=recent_dead_ends,
            confidence="medium",
            source="dead_end_review",
        )

    classes = tuple(dict.fromkeys(str(c).strip() for c in untried_classes if str(c).strip()))
    # Measured on 15,111 real shell invocations: the keyword detector matches a
    # technique class in only 4.1% of command lines, so "every class is
    # untried" is usually the *detector* being blind, not the run having tried
    # nothing.  Reporting all fifteen as a coverage gap is a checklist, not a
    # direction, and it is what every unsolved Run was getting.  Say what is
    # actually true instead.
    if classes and len(classes) >= len(VULN_CLASSES):
        return NextDirection(
            action="先补一次带明确技术特征的探针，让本轮攻击面可被识别",
            rationale=(
                "本轮没有任何可识别的攻击类命中记录，既不能证明试过哪些类，"
                "也不能据此断言缺口在哪；先产出带技术特征的证据（请求方法、参数、载荷形态），"
                "再谈研究方向。"
            ),
            evidence=_bounded_evidence((f"untried_classes={len(classes)}", f"total_facts={total_facts}", f"command_count={command_count}")),
            confidence="low",
            source="coverage_unknown",
        )
    if classes:
        # Four is the most a person can act on at once, and the action string
        # has to stay readable in a 390px sidebar.
        shown = classes[:4]
        return NextDirection(
            action=(
                f"先确认目标是否具备以下未尝试类的前置条件：{'、'.join(shown)}，"
                "再决定是否投入测试"
            ),
            rationale=(
                "这些类是整个运行从未尝试过的覆盖缺口，不是针对本靶标的推荐；"
                "它们只应作为排查清单，先用前置条件筛掉不适用项，避免为了覆盖而覆盖。"
            ),
            evidence=_bounded_evidence(shown),
            confidence="medium" if len(shown) >= 2 else "low",
            source="untried_class",
        )

    if total_facts <= 0:
        return NextDirection(
            action="用一次能落地的探针重新取事实",
            rationale=(
                "整个运行没有沉淀任何可用事实，任何方向都只是猜测；"
                "先用一次可观测、可复现的探针把事实拿回来。"
            ),
            evidence=_bounded_evidence((f"total_facts={total_facts}", f"command_count={command_count}")),
            confidence="low",
            source="no_fact",
        )

    return NextDirection(
        action="暂不下结论：先补一条可区分假设的观测，再给出研究方向",
        rationale=(
            "现有事实不足以区分候选攻击面，也没有候选 flag、未尝试类或死路可供排序；"
            "缺少的是能证伪其中一条假设的观测。"
        ),
        evidence=_bounded_evidence((f"total_facts={total_facts}", f"command_count={command_count}")),
        confidence="low",
        source="insufficient_evidence",
    )
