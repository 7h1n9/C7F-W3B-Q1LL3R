"""Dependency-free BM25 retrieval over curated security knowledge."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from app.knowledge.config import KnowledgeConfig, load_knowledge_config
from app.knowledge.loader import load_knowledge_items
from app.knowledge.models import KnowledgeItem, RetrievalRecord, RetrievalResult

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_.:][a-z0-9]+)*")
_TOKEN_PART_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "and",
        "challenge",
        "for",
        "http",
        "https",
        "if",
        "of",
        "on",
        "target",
        "test",
        "the",
        "to",
        "type",
        "url",
        "web",
        "with",
    }
)
_GENERIC_METADATA_TERMS = frozenset({"web", "target", "url", "http", "https"})
_CATEGORY_ALIASES = {
    "SSRF": {"ssrf", "server-side-request-forgery", "request-forgery"},
    "SQLI": {"sqli", "sql", "sql-injection", "database"},
    "COMMAND_INJECTION": {"command-injection", "os-command-injection", "shell"},
    "PATH_TRAVERSAL": {"path-traversal", "traversal", "file-read"},
    "FILE_UPLOAD": {"file-upload", "upload"},
    "IDOR": {"idor", "authorization", "access-control", "object-reference"},
    "SSTI": {"ssti", "template-injection", "server-side-template-injection"},
    "XSS": {"xss", "cross-site-scripting"},
    "XXE": {"xxe", "xml-external-entity"},
}
_VALID_KINDS = frozenset({"technique", "case", "poc", "negative"})

# A curated entry that names a CVE is prior knowledge for THAT vulnerability.
# Case-insensitive, and the trailing group is left open because CVE numbers are
# already past five digits.
_CVE_RE = re.compile(r"cve[-_ ]?(\d{4})[-_ ]?(\d{4,7})", re.IGNORECASE)
# Naming the same CVE the challenge names is worth far more than any lexical
# overlap: BM25 cannot tell CVE-2017-10271 from CVE-2018-2894 and will happily
# rank the neighbouring one first.
_CVE_MATCH_BOOST = 40.0

# Repository/URL boilerplate that rides along in every vulhub-style description.
# These tokens are what let an unrelated writeup score 62 on a query it has
# nothing to do with -- see the module note in ``retrieve``.
_BOILERPLATE_QUERY_TOKENS = frozenset(
    {
        "blob",
        "com",
        "github",
        "githubusercontent",
        "http",
        "https",
        "master",
        "net",
        "org",
        "raw",
        "readme",
        "tree",
        "vulhub",
        "www",
    }
)


def _is_boilerplate(token: str) -> bool:
    """True when a matched token carries no topical information.

    ``tokenize`` emits both the compound and its parts, so ``github.com`` has to
    be recognised through ``github`` + ``com`` rather than listed separately for
    every host that might appear.
    """

    if token in _BOILERPLATE_QUERY_TOKENS:
        return True
    if not any(separator in token for separator in ("-", "_", ".", ":")):
        return False
    parts = _TOKEN_PART_RE.findall(token)
    return bool(parts) and all(part in _BOILERPLATE_QUERY_TOKENS for part in parts)
_K1 = 1.5
_B = 0.75
_MAX_PRIOR_CHARS = 4000
# A Worker turn is bounded far tighter than a Reason pass; keep the worker
# block small enough to sit inside one tool-action prompt without crowding out
# the Blackboard context the Worker actually acts on.
_MAX_WORKER_PRIOR_CHARS = 1600


def tokenize(text: str) -> list[str]:
    """Normalize ASCII and CJK text while preserving compound technical tokens."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalized):
        token = match.group(0).strip("-_.")
        if not token:
            continue
        tokens.append(token)
        if any(separator in token for separator in ("-", "_", ".", ":")):
            tokens.extend(_TOKEN_PART_RE.findall(token))

    # Chinese has no ASCII word boundaries.  Character bigrams are cheap and
    # keep BM25 useful without adding an external segmentation dependency.
    for match in _CJK_RE.finditer(normalized):
        chunk = match.group(0)
        if len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(f"{chunk[index]}{chunk[index + 1]}" for index in range(len(chunk) - 1))
    return tokens


def search_tokens(text: str) -> list[str]:
    """Return retrieval query tokens with low-information route words removed."""

    return [token for token in tokenize(text) if token not in _SEARCH_STOPWORDS]


def cve_ids(text: str) -> set[str]:
    """Normalised ``CVE-YYYY-NNNNN`` ids named anywhere in ``text``.

    Two different CVEs can share a product, a version range and half a
    vulnerability class, so lexical retrieval alone cannot tell them apart —
    which is precisely how a WebLogic XMLDecoder challenge came to be attacked
    with the console path of a different CVE.  The id is the one identifier that
    is exact, so it is extracted and matched literally.
    """

    return {
        f"cve-{year}-{number.lower()}"
        for year, number in _CVE_RE.findall(str(text or "").lower())
    }


def format_prior(results: list[RetrievalResult]) -> str:
    """Format retrieval results as a compact, explicitly unverified PRIOR block."""

    if not results:
        return (
            "Knowledge prior: no relevant items found. "
            "This is a 待验证先验, not a VerifiedFact; continue from observed evidence."
        )

    lines = [
        "Knowledge prior (待验证先验，不是 VerifiedFact):",
        "Use these items only to propose hypotheses. Validate every claim against the target.",
    ]
    for result in results:
        item = result.item
        lines.append("")
        lines.append(f"[{item.id}] {_compact(item.title)}")
        lines.append(f"Technique: {_compact(item.technique)}")
        lines.append(f"Retrieval score: {result.score:.4f}")
        lines.append(f"适用条件: {_format_list(item.preconditions)}")
        lines.append("Steps:")
        if item.steps:
            for index, step in enumerate(item.steps[:8], start=1):
                lines.append(f"  {index}. {_compact(step)}")
            if len(item.steps) > 8:
                lines.append("  ... additional steps omitted")
        else:
            lines.append("  No steps recorded.")
        lines.append(f"验证方式: {_format_list(item.verification_method)}")
        lines.append(f"禁项/已知失败: {_format_list(item.known_failures)}")
        payload = _payload_block(item.payload_template, limit=400)
        if payload:
            lines.append("Payload sketch:")
            lines.extend(f"  {line}" for line in payload)

    rendered = "\n".join(lines)
    if len(rendered) <= _MAX_PRIOR_CHARS:
        return rendered
    return rendered[: _MAX_PRIOR_CHARS - 20].rstrip() + "\n[prior truncated]"


def format_worker_prior(
    results: list[RetrievalResult],
    *,
    max_chars: int = _MAX_WORKER_PRIOR_CHARS,
) -> str:
    """Format retrieval results as a compact, action-oriented Worker block.

    A Worker turn carries a much smaller prompt budget than the Coordinator
    Reason pass, so this keeps only what changes a decision: the technique
    identity, its first concrete steps, how to verify it, and the curated
    negative constraints.  It stays a 待验证先验 and never a VerifiedFact.
    """

    if not results:
        return ""

    lines = [
        "Curated technique prior for THIS direction (待验证先验, NOT a VerifiedFact):",
        "Use these as hypotheses and validate every claim against the real target.",
    ]
    for result in results:
        item = result.item
        lines.append(f"- [{item.id}] {_compact(item.title)} :: {_compact(item.technique)}")
        for index, step in enumerate(item.steps[:3], start=1):
            lines.append(f"  {index}) {_compact(step)}")
        verification = _format_list(item.verification_method, limit=2)
        if verification != "Not recorded.":
            lines.append(f"  verify: {verification}")
        failures = _format_list(item.known_failures, limit=2)
        if failures != "Not recorded.":
            lines.append(f"  avoid: {failures}")
        payload = _payload_block(item.payload_template, limit=300, max_lines=6)
        if payload:
            lines.append("  payload:")
            lines.extend(f"    {line}" for line in payload)

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    suffix = "\n[prior truncated]"
    return rendered[: max(max_chars - len(suffix), 0)].rstrip() + suffix


class KnowledgeRetriever:
    """Rank curated knowledge items with BM25 plus metadata compatibility boosts."""

    def __init__(
        self,
        items: Iterable[KnowledgeItem] | None = None,
        root: str | Path | None = None,
        config: KnowledgeConfig | None = None,
    ) -> None:
        self.config = config if config is not None else load_knowledge_config()
        self.root = Path(root).expanduser() if root is not None else self.config.root
        self.items = list(items) if items is not None else load_knowledge_items(self.root)

        self._document_tokens = [Counter(tokenize(_document_text(item))) for item in self.items]
        self._document_lengths = [sum(tokens.values()) for tokens in self._document_tokens]
        # The id carries the CVE for every entry that is *about* one --
        # ``cve-2016-5734-…``, ``xpoc:CVE-2022-21661`` -- while ``_document_text``
        # deliberately leaves the id out so it does not pollute BM25 with slug
        # fragments.  Reading the id only for CVE extraction keeps both:
        # measured on the imported xray corpus, without this a query for
        # CVE-2022-21661 returned CVE-2022-1950 and CVE-2022-0434 ahead of it,
        # because none of the three had a detectable CVE and the "a different
        # CVE is a different vulnerability" guard therefore never fired.
        self._document_cves = [
            cve_ids(_document_text(item)) | cve_ids(item.id) for item in self.items
        ]
        self._average_document_length = (
            sum(self._document_lengths) / len(self._document_lengths)
            if self._document_lengths
            else 1.0
        )
        self._idf = self._build_idf()
        self._log: RetrievalRecord | None = None

    def retrieve(
        self,
        *,
        query: str,
        metadata: dict | None = None,
        top_k: int = 3,
    ) -> list[RetrievalResult]:
        """Return positive-scoring items in a stable, reproducible order.

        Two guards sit on top of the BM25 score, both added after the same
        failure: a WebLogic XMLDecoder challenge (CVE-2017-10271) was attacked
        with the console path of a different CVE, because the corpus has no
        entry for 10271 and retrieval happily substituted the closest
        neighbour — a WebLogic SSRF writeup scoring 62 on nothing but
        "weblogic" plus the ``github.com/vulhub/tree/master`` boilerplate every
        vulhub description carries.

        * **CVE identity** — an entry that names a CVE is only prior knowledge
          for that CVE.  A different CVE is not a weaker match, it is a
          different vulnerability, so the entry is dropped outright.
        * **A discriminative match** — boilerplate repository/URL tokens are not
          evidence of relevance, so an entry that matched only those is dropped
          instead of being presented to the Reason model as a prior.
        """

        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k must be an integer >= 1; got {top_k!r}")

        normalized_metadata = dict(metadata or {})
        if self.config.mode == "off":
            results: list[RetrievalResult] = []
            self._save_log(query, normalized_metadata, top_k, results)
            return results

        query_tokens = search_tokens(query)
        query_term_frequencies = Counter(query_tokens)
        query_cves = cve_ids(query)
        requested_kinds = _requested_kinds(normalized_metadata.get("kind"))
        requested_category = _normalized_category(normalized_metadata.get("vulnerability_type"))
        require_published_at = bool(getattr(self.config, "require_published_at", False))

        ranked: list[tuple[float, KnowledgeItem, list[str], int]] = []
        for index, item in enumerate(self.items):
            if requested_kinds and item.kind not in requested_kinds:
                continue
            if require_published_at and item.published_at is None:
                # Excluded by configuration: an undated entry cannot be placed
                # on the correct side of a time-held-out split, so keeping it
                # would make the generalisation experiment silently meaningless.
                continue

            item_cves = self._document_cves[index]
            if query_cves and item_cves and not (item_cves & query_cves):
                # Named a *different* CVE: a different vulnerability that merely
                # shares a product name.  Ranking it as the top prior is what
                # sends the Worker down the wrong exploit path.
                continue

            document_tokens = self._document_tokens[index]
            score = self._bm25_score(query_term_frequencies, document_tokens, index)
            query_matches = _ordered_unique(
                term for term in query_tokens if document_tokens.get(term, 0) > 0
            )
            metadata_score, metadata_matches = _metadata_boost(item, normalized_metadata)
            if requested_category and requested_category in _category_tags(item):
                # A category match on a ``technique`` entry is worth more than the
                # same match on a ``poc`` entry, because the two answer different
                # questions.  A technique entry says "here is how this class of
                # weakness is found and confirmed", which is what a Worker needs
                # when it is deciding what to try; a poc entry says "here is one
                # CVE's request", which only helps once the CVE is already known.
                #
                # Measured on the ping/command-injection prior: with a flat +6.0
                # the imported xpoc entries took the top three slots on nothing but
                # a shared category tag, and the entry actually describing how to
                # test for command injection ranked fourth.  The Worker was handed
                # three unrelated CVEs and no method.
                boost = 9.0 if item.kind == "technique" else 6.0
                metadata_score += boost
                metadata_matches.append(requested_category)
            score += metadata_score

            cve_matches = sorted(query_cves & item_cves)
            if cve_matches:
                score += _CVE_MATCH_BOOST * len(cve_matches)
                metadata_matches.extend(cve_matches)

            if score <= 0:
                continue

            # Boilerplate alone is not relevance.  Without this, every writeup
            # that ever linked a GitHub README outranks an entry that actually
            # describes the vulnerability.  A declared metadata signal (an
            # explicit vulnerability type, a named technique) still qualifies on
            # its own: that is the caller telling us what it is looking at, not
            # a lexical coincidence.
            discriminative = [term for term in query_matches if not _is_boilerplate(term)]
            if not discriminative and not cve_matches and not metadata_matches:
                continue

            matched_terms = _ordered_unique([*query_matches, *metadata_matches])
            ranked.append((score, item, matched_terms, len(discriminative)))

        ranked.sort(key=lambda row: (-row[0], -row[3], row[1].id))
        results = [
            RetrievalResult(item=item, score=score, matched_terms=matched_terms)
            for score, item, matched_terms, _ in ranked[:top_k]
        ]
        self._save_log(query, normalized_metadata, top_k, results)
        return results

    def format_prior(self, results: list[RetrievalResult]) -> str:
        """Format results using the module-level compact formatter."""

        return format_prior(results)

    def last_log(self) -> RetrievalRecord | None:
        """Return the most recent retrieval record, including empty and off-mode runs."""

        return self._log

    def _build_idf(self) -> dict[str, float]:
        document_count = len(self.items)
        if document_count == 0:
            return {}

        document_frequency: Counter[str] = Counter()
        for tokens in self._document_tokens:
            document_frequency.update(tokens.keys())

        return {
            term: math.log(
                1.0 + (document_count - frequency + 0.5) / (frequency + 0.5)
            )
            for term, frequency in document_frequency.items()
        }

    def _bm25_score(
        self,
        query_term_frequencies: Counter[str],
        document_tokens: Counter[str],
        document_index: int,
    ) -> float:
        score = 0.0
        document_length = self._document_lengths[document_index]
        for term, query_frequency in query_term_frequencies.items():
            term_frequency = document_tokens.get(term, 0)
            if term_frequency <= 0:
                continue

            inverse_document_frequency = self._idf.get(term, 0.0)
            length_normalization = 1.0 - _B + _B * (
                document_length / self._average_document_length
            )
            numerator = term_frequency * (_K1 + 1.0)
            denominator = term_frequency + _K1 * length_normalization
            score += (
                query_frequency
                * inverse_document_frequency
                * numerator
                / denominator
            )
        return score

    def _save_log(
        self,
        query: str,
        metadata: dict,
        top_k: int,
        results: list[RetrievalResult],
    ) -> None:
        self._log = RetrievalRecord(
            query=query,
            metadata=metadata,
            top_k=top_k,
            results=results,
            searched_at=datetime.now(timezone.utc),
        )


def create_retriever(config: KnowledgeConfig | None = None) -> KnowledgeRetriever:
    """Build a retriever from explicit or environment-derived configuration."""

    resolved_config = config if config is not None else load_knowledge_config()
    return KnowledgeRetriever(config=resolved_config)


def _document_text(item: KnowledgeItem) -> str:
    fields: list[str] = [
        item.title,
        item.technique,
        item.summary,
        *item.preconditions,
        *item.target_stack,
        *item.steps,
        *item.known_failures,
    ]
    return "\n".join(field for field in fields if field)


def _normalized_category(value: object) -> str:
    raw = " ".join(_string_values(value)).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "SQL": "SQLI",
        "SQL_INJECTION": "SQLI",
        "COMMAND": "COMMAND_INJECTION",
        "OS_COMMAND_INJECTION": "COMMAND_INJECTION",
        "TRAVERSAL": "PATH_TRAVERSAL",
        "PATH_TRAVERSAL": "PATH_TRAVERSAL",
        "UPLOAD": "FILE_UPLOAD",
        "FILE_UPLOAD": "FILE_UPLOAD",
        "IDOR": "IDOR",
        "SSTI": "SSTI",
    }
    return aliases.get(raw, raw)


def _category_tags(item: KnowledgeItem) -> set[str]:
    tokens = set(tokenize(" ".join([item.title, item.technique, item.summary, *item.target_stack])))
    tags: set[str] = set()
    for category, aliases in _CATEGORY_ALIASES.items():
        category_tokens = tokenize(category.replace("_", " "))
        if any(alias in tokens for alias in aliases) or all(term in tokens for term in category_tokens):
            tags.add(category)
    if item.id.startswith("sqli-"):
        tags.add("SQLI")
    if item.id == "idor-object-reference":
        tags.add("IDOR")
    if item.id == "command-injection-parameter":
        tags.add("COMMAND_INJECTION")
    if item.id == "path-traversal-file-read":
        tags.add("PATH_TRAVERSAL")
    if item.id == "ssti-jinja2-detection":
        tags.add("SSTI")
    return tags


def _requested_kinds(value: object) -> set[str]:
    requested = {part.lower() for part in _string_values(value)}
    return requested & _VALID_KINDS


def _metadata_boost(
    item: KnowledgeItem,
    metadata: Mapping[str, object],
) -> tuple[float, list[str]]:
    technique_terms = set(tokenize(item.technique))
    target_terms = set(tokenize(" ".join(item.target_stack)))
    challenge_terms = technique_terms | target_terms | {item.kind}

    boost = 0.0
    matches: list[str] = []
    field_specs = (
        ("technique", technique_terms, 0.45),
        ("target_stack", target_terms, 0.50),
        ("target", target_terms, 0.50),
        ("challenge_type", challenge_terms, 0.25),
    )
    for key, document_terms, weight in field_specs:
        if key not in metadata:
            continue
        requested_terms = set(tokenize(" ".join(_string_values(metadata[key]))))
        # WEB_TARGET and route-like URL text contain no vulnerability intent.
        # Allowing them to overlap generic "web applications" corpus text was
        # the dominant source of identical, irrelevant top-3 rankings.
        requested_terms -= _GENERIC_METADATA_TERMS
        overlap = requested_terms & document_terms
        if overlap:
            boost += weight * len(overlap)
            matches.extend(sorted(overlap))
    return boost, _ordered_unique(matches)


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple, set, frozenset)):
        values: list[str] = []
        for part in value:
            stripped = str(part).strip()
            if stripped:
                values.append(stripped)
        return values
    return []


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _format_list(values: list[str], *, limit: int = 6) -> str:
    if not values:
        return "Not recorded."
    formatted = [_compact(value) for value in values[:limit]]
    if len(values) > limit:
        formatted.append(f"... {len(values) - limit} more omitted")
    return " | ".join(formatted)


def _payload_block(
    payload: str | None,
    *,
    limit: int,
    max_lines: int = 12,
) -> list[str]:
    """Render a stored payload sketch without collapsing its line structure.

    ``payload_template`` exists in the item schema and was never rendered, so a
    retrieved PoC arrived as prose while the one field a Worker could copy
    verbatim stayed invisible.  Line breaks are preserved (unlike ``_compact``)
    and cut on a line boundary so a truncated block never ends mid-token.
    """

    text = str(payload or "").strip()
    if not text:
        return []
    lines: list[str] = []
    used = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if used + len(line) > limit or len(lines) >= max_lines:
            lines.append("...")
            break
        lines.append(line[:limit])
        used += len(line) + 1
    return lines


def _compact(value: str) -> str:
    compacted = " ".join(value.split())
    if len(compacted) <= 320:
        return compacted
    return compacted[:317].rstrip() + "..."
