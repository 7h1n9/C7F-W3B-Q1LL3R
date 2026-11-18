from __future__ import annotations

import json
import logging
import re
from copy import copy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

KNOWLEDGE_PRIOR_WARNING = (
    "Knowledge results are unverified PRIOR information, not VerifiedFact evidence. "
    "Use them only as hypotheses and independently validate them in this Run before "
    "promoting any claim."
)

_FLAG_VALUE_PATTERN = re.compile(r"(?i)\b(?:flag|ctf)\{[^{}\r\n]{1,256}\}")
_MAX_FORMATTED_PRIOR_CHARS = 6_000
_MAX_DESCRIPTION_CHARS = 1_200
_MAX_HYPOTHESES = 5
_MAX_WORKER_PRIOR_CHARS = 1_600
# Distilled-trajectory templates are lower trust than the curated corpus, so
# they never take more than a fraction of the prior budget; the curated,
# human-reviewed block keeps the remainder.
_MAX_LEARNED_PRIOR_CHARS = 600
_LEARNED_PRIOR_WORKER_SHARE = 4
_LEARNED_PRIOR_REASON_SHARE = 5
# Cross-challenge lessons (CAP-11) are the second self-produced block, so they
# get a smaller slice than the distilled trajectories they follow.
_MAX_PROFILE_PRIOR_CHARS = 600
_PROFILE_PRIOR_WORKER_SHARE = 6
_PROFILE_PRIOR_REASON_SHARE = 6

#: Markers a truncated block ends with.  Trimmed text keeps its marker so the
#: result reads as "deliberately shortened" rather than "cut off mid-word".
_PRIOR_TRUNCATION_MARKERS = (
    "\n[learned prior truncated]",
)


def _clamp_prior_to_budget(text: str, max_chars: int) -> str:
    """Hard-clamp an assembled prior to ``max_chars``.

    The per-channel budgets inside :meth:`KnowledgeContextService.build_worker_prior`
    are floors, so two of them plus the separators joining the blocks can exceed
    the caller's cap (a 400-char budget produced 401).  This is the final
    enforcement; it only ever fires for budgets small enough that the floors
    conflict, which the production default of 1600 is not.
    """

    body = str(text or "")
    if max_chars <= 0:
        return ""
    if len(body) <= max_chars:
        return body
    for marker in _PRIOR_TRUNCATION_MARKERS:
        if body.endswith(marker):
            room = max_chars - len(marker)
            if room > 0:
                return body[: room] + marker
    return body[:max_chars]

_STACK_KEYS = (
    "confirmed_target_stack",
    "target_stack",
    "confirmed_stack",
    "technology_stack",
    "technologies",
    "stack",
)
_HYPOTHESIS_KEYS = (
    "ranked_hypotheses",
    "recent_hypotheses",
    "active_hypotheses",
    "active_hypotheses_json",
)
_CONFIRMED_FACT_KEYS = (
    "confirmed_facts",
    "confirmed_facts_json",
)
_CAPABILITY_LEDGER_KEYS = (
    "capability_ledger",
    "capability_ledger_json",
)
_SAFE_FACT_TYPES = frozenset(
    {
        "DBMS",
        "DATABASE",
        "FRAMEWORK",
        "LANGUAGE",
        "RUNTIME",
        "SERVER",
        "STACK",
        "TARGET_STACK",
        "TECH",
        "TECHNOLOGY",
        "TECHNOLOGIES",
        "WEB_SERVER",
    }
)
_CATEGORY_QUERY_TERMS = {
    "SSRF": ("server-side request forgery", "SSRF"),
    "SQLI": ("SQL injection", "boolean oracle"),
    "COMMAND_INJECTION": ("OS command injection", "command argument"),
    "PATH_TRAVERSAL": ("path traversal", "file read"),
    "FILE_UPLOAD": ("unrestricted file upload",),
    "IDOR": ("insecure direct object reference", "authorization bypass"),
    "SSTI": ("server-side template injection", "SSTI"),
    "XSS": ("cross-site scripting", "XSS"),
    "XXE": ("XML external entity", "XXE"),
}
_VULNERABILITY_SIGNALS = (
    ("SSTI", ("template", "模板", "渲染", "jinja", "server-side template")),
    ("COMMAND_INJECTION", ("command", "命令注入", "执行命令", "连通性", "诊断", "ping", "traceroute", "网络设备")),
    ("PATH_TRAVERSAL", ("path traversal", "路径穿越", "路径遍历", "文件读取", "预览", "preview", "file read")),
    ("IDOR", ("越权", "权限", "授权", "日志", "归档", "导出", "下载", "历史", "对象", "记录", "任务", "审核", "转换", "authorization", "idor", "export", "download", "archive", "object", "job")),
    ("SQLI", ("sql", "数据库注入", "sql injection")),
    ("FILE_UPLOAD", ("upload", "上传", "附件", "file upload")),
    ("SSRF", ("ssrf", "请求伪造", "server-side request", "webhook", "url fetch")),
    ("XSS", ("xss", "cross-site scripting", "脚本注入")),
    ("XXE", ("xxe", "xml external entity", "xml")),
)


def load_knowledge_config() -> Any:
    """Load knowledge configuration lazily so the off path imports no retriever."""
    from app.knowledge.config import load_knowledge_config as loader

    return loader()


def resolve_knowledge_mode(run_hints: dict[str, Any], default: str) -> Any:
    from app.knowledge.config import resolve_knowledge_mode as resolver

    return resolver(run_hints, default)


def create_retriever(config: Any) -> Any:
    """Create a retriever only after an on-mode decision has been made."""
    from app.knowledge.retriever import create_retriever as factory

    return factory(config)


# The curated corpus is shipped with the application and does not change while
# a process is running, so one BM25 index per (root, top_k) is enough.  Without
# this cache every Worker turn and every Reason cycle re-read the JSONL corpus
# and re-tokenized all documents.  Only real KnowledgeRetriever instances are
# cached, so test doubles injected through ``create_retriever`` stay untouched.
_RETRIEVER_CACHE: dict[tuple[str, int], Any] = {}


def _cached_retriever(config: Any) -> Any:
    """Return a process-cached retriever built through the module factory."""

    from app.knowledge.retriever import KnowledgeRetriever

    key = (
        str(_get_value(config, "root")),
        _config_top_k(config),
        bool(_get_value(config, "require_published_at", default=False)),
    )
    cached = _RETRIEVER_CACHE.get(key)
    if isinstance(cached, KnowledgeRetriever):
        return cached
    retriever = create_retriever(config)
    if isinstance(retriever, KnowledgeRetriever):
        _RETRIEVER_CACHE[key] = retriever
    return retriever


def clear_retriever_cache() -> None:
    """Drop the cached BM25 indexes (used by tests and corpus reloads)."""

    _RETRIEVER_CACHE.clear()


def _get_value(source: Any, *names: str, default: Any = None) -> Any:
    if source is None:
        return default
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _safe_text(value: Any, *, limit: int | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(value)
    text = _FLAG_VALUE_PATTERN.sub("[REDACTED_FLAG]", text).strip()
    if limit is not None:
        return text[:limit]
    return text


def _dedupe(values: list[str], *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _safe_text(value).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _direct_stack_values(source: Any) -> list[str]:
    values: list[str] = []
    for key in _STACK_KEYS:
        for value in _as_list(_get_value(source, key)):
            values.extend(_as_list(value))
    return _dedupe(values)


def _facts_stack_values(facts: Any) -> list[str]:
    values: list[str] = []
    for fact in _as_list(facts):
        if not isinstance(fact, dict):
            continue
        values.extend(_direct_stack_values(fact))
        fact_type = _safe_text(fact.get("type") or fact.get("category") or fact.get("key")).upper()
        if fact_type in _SAFE_FACT_TYPES:
            value = fact.get("value") or fact.get("name") or fact.get("statement")
            values.extend(_as_list(value))
    return values


def _target_stack(state: Any, solver_state: Any, challenge: Any) -> list[str]:
    values: list[str] = []
    for source in (solver_state, state):
        values.extend(_direct_stack_values(source))
        for key in _CONFIRMED_FACT_KEYS:
            values.extend(_facts_stack_values(_get_value(source, key)))
        for key in _CAPABILITY_LEDGER_KEYS:
            ledger = _get_value(source, key)
            if not isinstance(ledger, dict):
                continue
            values.extend(_direct_stack_values(ledger))
            for fact_key in _CONFIRMED_FACT_KEYS:
                values.extend(_facts_stack_values(ledger.get(fact_key)))

    metadata = _get_value(challenge, "metadata_json", default={})
    if isinstance(metadata, dict):
        # Only explicitly stack-shaped keys are eligible. Never pass arbitrary
        # challenge metadata because it can contain solutions or ground truth.
        values.extend(_direct_stack_values(metadata))
        for key in ("framework", "server", "database", "dbms", "language"):
            values.extend(_as_list(metadata.get(key)))
    return _dedupe(values, limit=12)


def _hypothesis_summary(hypothesis: Any) -> str:
    statement = _safe_text(
        _get_value(hypothesis, "statement", "title", "description"),
        limit=260,
    )
    if not statement:
        return ""
    category = _safe_text(_get_value(hypothesis, "category", "type"), limit=80)
    status = _safe_text(_get_value(hypothesis, "status"), limit=40)
    confidence = _get_value(hypothesis, "confidence")
    details = [item for item in (category, status) if item]
    if isinstance(confidence, (int, float)):
        details.append(f"confidence={confidence}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{statement}{suffix}"


def _recent_hypotheses(state: Any, solver_state: Any) -> list[str]:
    summaries: list[str] = []
    for source in (solver_state, state):
        for key in _HYPOTHESIS_KEYS:
            for hypothesis in _as_list(_get_value(source, key))[-_MAX_HYPOTHESES:]:
                summaries.append(_hypothesis_summary(hypothesis))
    return _dedupe(summaries, limit=_MAX_HYPOTHESES)


def _normalized_mode(value: Any) -> str:
    if hasattr(value, "mode"):
        value = value.mode
    text = str(value or "off").strip().lower()
    return "on" if text in {"on", "enabled", "true", "1"} else text or "off"


def _config_for_mode(config: Any, mode: str) -> Any:
    """Return a retriever config whose mode reflects the resolved Run mode."""
    if _normalized_mode(_get_value(config, "mode")) == mode:
        return config
    try:
        return replace(config, mode=mode)
    except TypeError:
        config_copy = copy(config)
        setattr(config_copy, "mode", mode)
        return config_copy


def _config_top_k(config: Any) -> int:
    value = _get_value(config, "top_k", "retrieval_top_k", default=3)
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 3


def _config_reason(config: Any) -> str:
    reason = _get_value(config, "disabled_reason", "reason")
    return _safe_text(reason, limit=200) or "disabled_by_environment"


def _host_from_url(target_url: str) -> str:
    if not target_url:
        return ""
    try:
        parsed = urlsplit(target_url)
    except ValueError:
        return _safe_text(target_url, limit=255)
    return parsed.netloc.rsplit("@", 1)[-1] or parsed.hostname or ""


def _vulnerability_categories(challenge: Any) -> list[str]:
    """Infer bounded technique categories from non-secret challenge semantics."""

    metadata = _get_value(challenge, "metadata_json", default={})
    explicit = ""
    if isinstance(metadata, dict):
        explicit = _safe_text(
            metadata.get("vulnerability_type") or metadata.get("technique_type"),
            limit=100,
        )
    text = " ".join(
        [
            explicit,
            _safe_text(_get_value(challenge, "name"), limit=300),
            _safe_text(_get_value(challenge, "description"), limit=600),
        ]
    ).casefold()
    if not text.strip():
        return []

    matched = [
        category
        for category, signals in _VULNERABILITY_SIGNALS
        if any(str(signal).casefold() in text for signal in signals)
    ]
    if explicit:
        from app.knowledge.retriever import _normalized_category

        normalized = _normalized_category(explicit)
        if normalized and normalized not in matched:
            matched.insert(0, normalized)
    return _dedupe(matched, limit=2)


def _text_categories(text: str) -> list[str]:
    """Infer bounded technique categories from arbitrary (non-secret) text."""

    folded = _safe_text(text, limit=800).casefold()
    if not folded.strip():
        return []
    return [
        category
        for category, signals in _VULNERABILITY_SIGNALS
        if any(str(signal).casefold() in folded for signal in signals)
    ]


def _worker_categories(challenge: Any, intent_goal: str) -> list[str]:
    """Rank categories by the Worker's own intent, then the Challenge semantics.

    The intent is the direction this Worker is actually executing, so it must
    outrank the challenge-level guess whenever the two disagree.
    """

    return _dedupe(
        [*_text_categories(intent_goal), *_vulnerability_categories(challenge)],
        limit=2,
    )


def _searched_at(retriever: Any) -> str:
    log = retriever.last_log() if hasattr(retriever, "last_log") else None
    value = _get_value(log, "searched_at")
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_text(value, limit=80) or datetime.now(UTC).isoformat()


def _template_category(challenge: Any) -> str:
    """The upstream category this challenge distils under.

    Must resolve to the same value the *writer* filed the template under.  A
    challenge is stored as ``WEB_TARGET`` but its template is filed under the
    upstream category ``web``; passing the raw type here compared ``WEB_TARGET``
    against ``web`` and cost every template the category term -- the single
    heaviest weight in the scorer -- so recall degraded to keyword-only, which
    then also failed on Chinese names.

    An explicitly carried ``category`` is only honoured when it is already one of
    the upstream categories.  A raw challenge type is mapped, so the same bug
    cannot reappear through a caller that happens to expose a ``category``
    attribute holding ``WEB_TARGET`` (which is exactly what the challenge model
    used to do before the field was dropped).
    """

    from app.solver.muteki.upstream_bridge import (
        category_for_challenge_type,
        upstream_categories,
    )

    explicit = _safe_text(_get_value(challenge, "category"), limit=80).lower()
    if explicit and explicit in upstream_categories():
        return explicit
    return category_for_challenge_type(
        _get_value(challenge, "challenge_type") or explicit
    )


def _template_keywords(*sources: str) -> list[str]:
    """Tokens used to match distilled template fingerprints.

    The distiller tokenises with ``[a-zA-Z][a-zA-Z0-9_]{2,}``, so the ASCII half
    of this has to keep that exact shape or an English token could never match.
    But that shape is also why recall used to fail on this project's own
    challenges: the arena catalogue is mostly Chinese-titled, and a regex that
    only accepts a leading ASCII letter extracts *nothing* from
    ``[CISCN 2019 初赛]Love Math`` beyond the Latin fragments -- the Chinese
    characters that actually identify the challenge are dropped.  CJK runs are
    therefore tokenised too.  Recall compares tokens by equality, so emitting
    them cannot collide with an English keyword; it only widens what can match.
    """

    tokens: list[str] = []
    for source in sources:
        text = _safe_text(source, limit=1_000).lower()
        # ASCII identifiers: same shape the distiller stores.
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text):
            if token not in tokens:
                tokens.append(token)
        # CJK runs: two or more Han characters.  Single characters are far too
        # common to be a fingerprint (they would match almost every template),
        # so they are deliberately excluded.
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            if token not in tokens:
                tokens.append(token)
    return tokens[:24]


def _learned_prior_detail(
    *,
    category: str,
    keywords: list[str],
    top_k: int,
    max_chars: int,
) -> tuple[str, list[str]]:
    """Recall this project's own distilled trajectories, and say which ones.

    Returns ``(block, names)``.  The names are not decoration: the recall is a
    self-produced, lower-trust channel that is capped to a fraction of the
    budget, and without them the durable event log records only the curated
    ``knowledge_item_ids`` -- so "did the learned prior actually reach the
    prompt, and with what?" could only be answered by reproducing the call by
    hand.  That is exactly what had to be done to discover that every challenge
    was being handed the same unrelated template.

    Every failure degrades to ``("", [])``: recall must never block a solver
    step.
    """

    if max_chars <= 0 or not (category or keywords):
        return "", []
    try:
        from app.knowledge.config import knowledge_templates_root
        from app.services.retrieve import TemplateRetriever, format_learned_prior
        from muteki.learning.distill import TemplateStore

        root = knowledge_templates_root()
        if not root.is_dir() or not any(root.glob("*.yaml")):
            return "", []
        templates = TemplateRetriever(TemplateStore(root=root)).retrieve(
            category=category,
            keywords=keywords,
            top_k=max(1, top_k),
        )
        names = [str(getattr(tpl, "name", "") or "") for tpl in templates]
        return _safe_text(format_learned_prior(templates, max_chars=max_chars)), names
    except Exception:
        logger.warning("Learned template recall failed; continuing without it", exc_info=True)
        return "", []


def _learned_prior_block(
    *,
    category: str,
    keywords: list[str],
    top_k: int,
    max_chars: int,
) -> str:
    """The recalled block alone, for callers that do not need the provenance."""

    block, _ = _learned_prior_detail(
        category=category, keywords=keywords, top_k=top_k, max_chars=max_chars
    )
    return block


class KnowledgeContextService:
    """Build an isolated, verifiable PRIOR block for one solver step."""

    @staticmethod
    def build(
        run: Any,
        challenge: Any,
        state: Any = None,
        solver_state: Any = None,
        profile_prior: str = "",
    ) -> dict[str, Any]:
        run_hints = getattr(run, "hints_json", {}) or {}
        if not isinstance(run_hints, dict):
            run_hints = {}

        raw_run_mode = _safe_text(run_hints.get("knowledge_mode")).lower()
        if raw_run_mode == "off":
            return {"mode": "off", "enabled": False, "reason": "disabled_by_run"}

        mode = raw_run_mode or "off"
        run_mode_configured = "knowledge_mode" in run_hints
        try:
            config = load_knowledge_config()
            default_mode = _get_value(config, "mode", default="off")
            resolved_mode = resolve_knowledge_mode(run_hints, str(default_mode))
            mode = _normalized_mode(resolved_mode)
            if mode != "on":
                return {
                    "mode": mode,
                    "enabled": False,
                    "reason": _config_reason(config),
                }

            challenge_type = _safe_text(_get_value(challenge, "challenge_type"), limit=100)
            target_url = _safe_text(_get_value(challenge, "target_url"), limit=2_048)
            current_phase = _safe_text(
                _get_value(solver_state, "current_phase")
                or _get_value(state, "current_phase")
                or _get_value(run, "current_phase"),
                limit=100,
            )
            allowed_hosts = _dedupe(
                [_safe_text(item, limit=255) for item in _as_list(_get_value(challenge, "allowed_hosts"))],
                limit=20,
            )
            stack = _target_stack(state, solver_state, challenge)
            hypotheses = _recent_hypotheses(state, solver_state)
            name = _safe_text(_get_value(challenge, "name"), limit=300)
            description = _safe_text(
                _get_value(challenge, "description"),
                limit=_MAX_DESCRIPTION_CHARS,
            )
            vulnerability_categories = _vulnerability_categories(challenge)
            category_query_terms = [
                term
                for category in vulnerability_categories
                for term in _CATEGORY_QUERY_TERMS.get(category, ())
            ]

            query_parts = [
                f"Challenge: {name}" if name else "",
                f"Current phase: {current_phase}" if current_phase else "",
                f"Confirmed target stack: {', '.join(stack)}" if stack else "",
                f"Recent hypotheses: {' | '.join(hypotheses)}" if hypotheses else "",
                f"Candidate vulnerability categories: {', '.join(vulnerability_categories)}" if vulnerability_categories else "",
                f"Relevant techniques: {', '.join(category_query_terms)}" if category_query_terms else "",
                f"Description: {description}" if description else "",
            ]
            query = "\n".join(item for item in query_parts if item)
            metadata = {
                "challenge_type": challenge_type,
                "target_url": target_url or None,
                "vulnerability_type": vulnerability_categories[0] if vulnerability_categories else None,
                "current_phase": current_phase or None,
                "allowed_hosts": allowed_hosts,
            }

            top_k = _config_top_k(config)
            config_mode = _normalized_mode(default_mode)
            resolved_config = _config_for_mode(config, mode)
            retriever = _cached_retriever(resolved_config)
            results = retriever.retrieve(query=query, metadata=metadata, top_k=top_k)
            serialized_results = [result.model_dump(mode="json") for result in results]
            # The two knowledge channels are merged here: curated BM25 items
            # (human-reviewed, higher trust) plus this project's own distilled
            # trajectories (lower trust, capped to a fraction of the budget).
            learned_prior, learned_names = _learned_prior_detail(
                category=_template_category(challenge),
                keywords=_template_keywords(
                    description, " ".join(vulnerability_categories), query
                ),
                top_k=min(top_k, 2),
                max_chars=min(
                    _MAX_LEARNED_PRIOR_CHARS,
                    max(_MAX_FORMATTED_PRIOR_CHARS // _LEARNED_PRIOR_REASON_SHARE, 200),
                ),
            )
            profile_block = _safe_text(
                profile_prior,
                limit=max(_MAX_FORMATTED_PRIOR_CHARS // _PROFILE_PRIOR_REASON_SHARE, 200),
            )
            self_produced = [block for block in (learned_prior, profile_block) if block]
            curated_budget = _MAX_FORMATTED_PRIOR_CHARS - sum(
                len(block) + 1 for block in self_produced
            )
            formatted_prior = _safe_text(retriever.format_prior(results))
            if len(formatted_prior) > curated_budget:
                suffix = "\n[Knowledge prior truncated; retrieved metadata remains available.]"
                content_limit = max(curated_budget - len(suffix), 0)
                formatted_prior = formatted_prior[:content_limit].rstrip() + suffix
            if self_produced:
                tail = "\n".join(self_produced)
                formatted_prior = (
                    f"{formatted_prior}\n{tail}"
                    if formatted_prior
                    else tail
                )
            return {
                "mode": "on",
                "on": True,
                "enabled": True,
                "query": query,
                "metadata": metadata,
                "top_k": top_k,
                "results": serialized_results,
                "formatted_prior": formatted_prior,
                "learned_prior": learned_prior,
                # Provenance of the self-produced channel, so the durable event
                # log can answer "which template did this Run actually see?".
                "learned_prior_names": learned_names,
                "learned_prior_chars": len(learned_prior),
                "profile_prior": profile_block,
                "warning": KNOWLEDGE_PRIOR_WARNING,
                "searched_at": _searched_at(retriever),
                "mode_source": "run_hints" if run_mode_configured else "environment",
                "run_override_applied": run_mode_configured and config_mode != mode,
            }
        except Exception as error:
            logger.exception("Knowledge augmentation failed; continuing without knowledge prior")
            return {
                "mode": mode,
                "on": mode == "on",
                "enabled": False,
                "error": f"{type(error).__name__}: {error}",
                # Present-but-empty keeps the caller's event payload shape
                # stable whether or not recall threw.
                "learned_prior": "",
                "learned_prior_names": [],
                "learned_prior_chars": 0,
            }

    @staticmethod
    def build_worker_prior(
        run: Any,
        challenge: Any,
        *,
        intent_goal: str = "",
        tool_name: str = "",
        state: Any = None,
        solver_state: Any = None,
        top_k: int | None = None,
        max_chars: int = _MAX_WORKER_PRIOR_CHARS,
        profile_prior: str = "",
    ) -> dict[str, Any]:
        """Build a bounded, intent-scoped PRIOR block for one Worker turn.

        ``build`` ranks against the Challenge once, so every Worker receives the
        same items no matter which direction it is executing.  A Worker turn is
        narrow, so this variant ranks against the Worker's own intent (goal +
        declared tool + phase + confirmed stack) and returns a compact block.
        The result is still a 待验证先验: hypotheses only, never a VerifiedFact.
        """

        run_hints = getattr(run, "hints_json", {}) or {}
        if not isinstance(run_hints, dict):
            run_hints = {}

        if _safe_text(run_hints.get("knowledge_mode")).lower() == "off":
            return {
                "mode": "off",
                "enabled": False,
                "prior": "",
                "reason": "disabled_by_run",
            }

        try:
            config = load_knowledge_config()
            default_mode = _get_value(config, "mode", default="off")
            mode = _normalized_mode(resolve_knowledge_mode(run_hints, str(default_mode)))
            if mode != "on":
                return {
                    "mode": mode,
                    "enabled": False,
                    "prior": "",
                    "reason": _config_reason(config),
                }

            from app.knowledge.retriever import format_worker_prior

            phase = _safe_text(
                _get_value(solver_state, "current_phase")
                or _get_value(state, "current_phase")
                or _get_value(run, "current_phase"),
                limit=100,
            )
            stack = _target_stack(state, solver_state, challenge)
            goal = _safe_text(intent_goal, limit=600)
            tool = _safe_text(tool_name, limit=80)
            categories = _worker_categories(challenge, goal)
            category_terms = [
                term
                for category in categories
                for term in _CATEGORY_QUERY_TERMS.get(category, ())
            ]

            query_parts = [
                f"Worker intent: {goal}" if goal else "",
                f"Declared tool: {tool}" if tool else "",
                f"Current phase: {phase}" if phase else "",
                f"Confirmed target stack: {', '.join(stack)}" if stack else "",
                f"Candidate vulnerability categories: {', '.join(categories)}" if categories else "",
                f"Relevant techniques: {', '.join(category_terms)}" if category_terms else "",
            ]
            query = "\n".join(part for part in query_parts if part)
            if not query.strip():
                return {
                    "mode": "on",
                    "enabled": False,
                    "prior": "",
                    "reason": "empty_worker_query",
                }

            resolved_top_k = top_k if top_k is not None else _config_top_k(config)
            if isinstance(resolved_top_k, bool) or not isinstance(resolved_top_k, int) or resolved_top_k < 1:
                resolved_top_k = _config_top_k(config)
            metadata = {
                "current_phase": phase or None,
                "vulnerability_type": categories[0] if categories else None,
                "challenge_type": _safe_text(_get_value(challenge, "challenge_type"), limit=100) or None,
            }
            retriever = _cached_retriever(_config_for_mode(config, mode))
            results = retriever.retrieve(query=query, metadata=metadata, top_k=resolved_top_k)
            # Merge the two channels: curated BM25 items keep the bulk of the
            # budget; the project's own distilled trajectories get a capped
            # slice, because they are self-generated and therefore lower trust.
            learned_prior = _learned_prior_block(
                category=_template_category(challenge),
                keywords=_template_keywords(goal, " ".join(categories), query),
                top_k=min(resolved_top_k, 2),
                max_chars=min(
                    _MAX_LEARNED_PRIOR_CHARS,
                    max(max_chars // _LEARNED_PRIOR_WORKER_SHARE, 200),
                ),
            )
            profile_block = _safe_text(
                profile_prior,
                limit=max(max_chars // _PROFILE_PRIOR_WORKER_SHARE, 200),
            )
            self_produced = [block for block in (learned_prior, profile_block) if block]
            curated_budget = max_chars - sum(len(block) + 1 for block in self_produced)
            prior = _safe_text(
                format_worker_prior(results, max_chars=max(curated_budget, 200))
            )
            if self_produced:
                tail = "\n".join(self_produced)
                prior = f"{prior}\n{tail}" if prior else tail
            # ``max_chars`` is a hard cap, not a hint: the caller sized it for a
            # prompt that has to fit.  The per-channel floors above exist so one
            # channel stays readable on its own, but they are floors rather than a
            # joint budget, so together with the separators they can overshoot --
            # which is how a 400-char budget assembled 401.  The result is
            # therefore clamped here, trimming the lower-trust tail and keeping its
            # truncation marker intact.
            prior = _clamp_prior_to_budget(prior, max_chars)
            return {
                "mode": "on",
                "enabled": bool(prior),
                "prior": prior,
                "learned_prior": learned_prior,
                "profile_prior": profile_block,
                "query": query,
                "top_k": resolved_top_k,
                "results": [result.model_dump(mode="json") for result in results],
                "item_ids": [result.item.id for result in results],
                "warning": KNOWLEDGE_PRIOR_WARNING,
                "searched_at": _searched_at(retriever),
            }
        except Exception as error:
            logger.exception("Worker knowledge prior failed; continuing without it")
            return {
                "mode": "on",
                "enabled": False,
                "prior": "",
                "error": f"{type(error).__name__}: {error}",
            }


knowledge_context_service = KnowledgeContextService()
