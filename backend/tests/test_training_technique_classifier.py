"""Tests for the offline technique classifier.

The model is stubbed out on purpose: this suite must prove the *contract*
(validation, caching, failure accounting, noise filtering) without a network
call, so it keeps passing when the upstream provider is degraded -- which is
exactly the state it was written in.

The load-bearing property throughout: a transport failure and a model judgement
of ``unknown`` are different facts and must never collapse into each other.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.training.technique_classifier import (
    ALLOWED_LABELS,
    MIN_COMMAND_CHARS,
    PROMPT_VERSION,
    ClassificationResult,
    TechniqueCache,
    classify_batch,
    command_hash,
    is_platform_noise,
    load_model_credentials,
    parse_response,
    resolve_model_entry,
)


# ---------------------------------------------------------------------------
# Stub transport
# ---------------------------------------------------------------------------


class StubResponse:
    def __init__(self, status_code: int, payload: object | None = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class StubClient:
    """Replays a queue of responses and records the requests it saw."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def post(self, url, *, headers=None, json=None, timeout=None):  # noqa: A002
        self.requests.append({"url": url, "json": json, "headers": headers})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(content: str) -> StubResponse:
    return StubResponse(200, {"choices": [{"message": {"content": content}}]})


def ok(labels, confidence=0.9, why="test") -> StubResponse:
    return reply(json.dumps({"labels": labels, "confidence": confidence, "why": why}))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_plain_json_reply_parses():
    result = parse_response('{"labels":["sqli.union"],"confidence":0.8,"why":"union"}', model="m")
    assert result.ok
    assert result.labels == ("sqli.union",)
    assert result.confidence == 0.8


def test_fenced_json_reply_parses():
    text = '```json\n{"labels":["lfi.wrapper"],"confidence":1}\n```'
    assert parse_response(text, model="m").labels == ("lfi.wrapper",)


def test_json_with_surrounding_prose_parses():
    text = 'Sure! {"labels":["rce.ognl"],"confidence":0.5} hope that helps'
    assert parse_response(text, model="m").labels == ("rce.ognl",)


def test_alias_answer_is_flagged_salvaged():
    # 28 rows once arrived looking clean while every one had come through an
    # alias field; without this flag the cache cannot tell them apart.
    result = parse_response(
        '{"classification":"reconnaissance","confidence":0.75,"reason":"probing"}', model="m"
    )
    assert result.ok and result.salvaged is True


def test_clean_schema_answer_is_not_flagged():
    result = parse_response(
        '{"labels":["sqli.union"],"confidence":0.9,"why":"union select"}', model="m"
    )
    assert result.ok and result.salvaged is False


def test_answer_without_rationale_is_flagged():
    result = parse_response('{"labels":["sqli.union"],"confidence":0.9}', model="m")
    assert result.ok and result.salvaged is True


def test_unknown_is_dropped_when_paired_with_a_real_label():
    # `["unknown","ssrf.internal"]` was observed on a Solr XXE probe; the
    # specific label carries more information than the abstention.
    result = parse_response(
        '{"labels":["unknown","ssrf.internal"],"confidence":0.7,"why":"xxe"}', model="m"
    )
    assert result.labels == ("ssrf.internal",)


def test_missing_confidence_defaults_to_lowest_usable():
    result = parse_response('{"labels":["unknown"],"why":"x"}', model="m")
    assert result.confidence == 0.3


def test_explicit_zero_confidence_is_preserved():
    # An explicit 0 is an assertion, not an omission.
    result = parse_response('{"labels":["unknown"],"confidence":0,"why":"x"}', model="m")
    assert result.confidence == 0.0


def test_out_of_vocabulary_label_is_rejected():
    # An invented label must fail loudly, not enter the miner's namespace.
    result = parse_response('{"labels":["magic_exploit"],"confidence":1}', model="m")
    assert not result.ok
    assert result.error.startswith("label_out_of_vocabulary")


def test_known_free_text_category_is_normalised():
    # Measured on glm-5.3-flash: asked for `labels`, it answers
    # `classification: "reconnaissance"`. Recovering that is the difference
    # between a usable dataset and a 74% discard rate.
    result = parse_response(
        '{"classification":"reconnaissance","confidence":0.75,"reason":"probing"}', model="m"
    )
    assert result.ok
    assert result.labels == ("recon.source",)
    assert result.confidence == 0.75


def test_category_alias_is_read():
    result = parse_response('{"category":"sql injection","confidence":0.8}', model="m")
    assert result.ok and result.labels == ("sqli.union",)


def test_compound_category_takes_the_first_mappable_part():
    result = parse_response(
        '{"classification":"reconnaissance / enumeration","confidence":0.6}', model="m"
    )
    assert result.ok and result.labels == ("recon.source",)


def test_alias_lookup_is_case_and_punctuation_insensitive():
    result = parse_response('{"labels":["File-Upload"],"confidence":0.7}', model="m")
    assert result.ok and result.labels == ("upload.bypass",)


def test_unknown_alias_words_are_still_rejected():
    # The alias table is a whitelist, not a fuzzy matcher: the vocabulary must
    # not drift just because the model invents a phrase.
    result = parse_response('{"classification":"weird new thing","confidence":0.9}', model="m")
    assert not result.ok
    assert "weird new thing" in result.error


def test_more_than_two_labels_is_rejected():
    # Bundling is the main way this classification becomes useless.
    result = parse_response(
        '{"labels":["sqli.union","sqli.boolean","lfi.traversal"],"confidence":1}', model="m"
    )
    assert not result.ok
    assert result.error == "too_many_labels"


def test_unparseable_reply_is_an_error_not_unknown():
    result = parse_response("I cannot help with that.", model="m")
    assert not result.ok
    assert result.error == "unparseable_json"


def test_empty_label_list_is_an_error():
    assert parse_response('{"labels":[],"confidence":1}', model="m").error == "no_labels"


def test_duplicate_labels_are_collapsed():
    result = parse_response('{"labels":["sqli.union","sqli.union"],"confidence":1}', model="m")
    assert result.ok and result.labels == ("sqli.union",)


def test_unknown_is_an_ordinary_allowed_label():
    result = parse_response('{"labels":["unknown"],"confidence":0.7}', model="m")
    assert result.ok and result.labels == ("unknown",)
    assert "unknown" in ALLOWED_LABELS


def test_confidence_is_clamped():
    assert parse_response('{"labels":["unknown"],"confidence":9}', model="m").confidence == 1.0
    assert parse_response('{"labels":["unknown"],"confidence":-3}', model="m").confidence == 0.0


# ---------------------------------------------------------------------------
# Hashing and noise filtering
# ---------------------------------------------------------------------------


def test_hash_ignores_whitespace_only():
    assert command_hash("curl  -s\n http://a") == command_hash("curl -s http://a")


def test_hash_separates_different_payloads():
    assert command_hash("curl http://a/?id=1") != command_hash("curl http://a/?id=2")


def test_short_commands_are_noise():
    assert is_platform_noise("ls")
    assert is_platform_noise("p" * (MIN_COMMAND_CHARS - 1))


def test_platform_bookkeeping_is_noise():
    assert is_platform_noise("cd /home/kali/workspace && python3 blackboard.py read-all")
    assert is_platform_noise("cd /home/kali/workspace && python3 blackboard.py read-facts")


def test_real_exploit_commands_are_not_noise():
    assert not is_platform_noise(
        "curl -s 'http://t/solr/demo/select?wt=xml&defType=xmlparser&q=x'"
    )


# ---------------------------------------------------------------------------
# Batch classification
# ---------------------------------------------------------------------------


def test_successful_batch_returns_labels_with_hashes():
    client = StubClient([ok(["sqli.union"]), ok(["unknown"])])
    results = run(
        classify_batch(
            client,
            ["curl 'http://a/?id=1 union select 1'", "cat /tmp/x"],
            base_url="http://x/v1",
            model="m",
            api_key="k",
        )
    )
    assert [r.labels for r in results] == [("sqli.union",), ("unknown",)]
    assert all(r.command_hash for r in results)
    assert results[0].command_hash == command_hash("curl 'http://a/?id=1 union select 1'")


def test_transport_error_is_recorded_not_swallowed():
    client = StubClient([RuntimeError("boom")])
    results = run(
        classify_batch(
            client, ["cmd one long enough here"],
            base_url="http://x/v1", model="m", api_key="k", max_retries=0,
        )
    )
    assert results[0].error.startswith("transport:RuntimeError")
    assert results[0].labels == ()


def test_transport_error_is_retried_then_succeeds():
    # On this relay a connect timeout is usually a blip, not a refusal.
    client = StubClient([RuntimeError("blip"), ok(["unknown"])])
    results = run(
        classify_batch(
            client, ["cmd one long enough here"],
            base_url="http://x/v1", model="m", api_key="k",
            max_retries=1, retry_backoff=0,
        )
    )
    assert results[0].ok
    assert results[0].labels == ("unknown",)


def test_prose_answer_is_salvaged_not_retried():
    # Prose that names a technique is recovered from the text, so no retry is
    # needed -- and the salvaged confidence is capped so it cannot outrank a
    # clean verdict when run counts are summed.
    client = StubClient([reply("This command attempts a Local File Inclusion test")])
    results = run(
        classify_batch(
            client, ["cmd one long enough here"],
            base_url="http://x/v1", model="m", api_key="k",
            max_retries=1, retry_backoff=0,
        )
    )
    assert results[0].labels == ("lfi.traversal",)
    assert results[0].confidence <= 0.4
    assert len(client.requests) == 1


def test_unsalvageable_prose_is_retried():
    # Nothing usable in the text, so a retry is worth the round trip.
    client = StubClient([reply("I cannot help with that."), ok(["lfi.wrapper"])])
    results = run(
        classify_batch(
            client, ["cmd one long enough here"],
            base_url="http://x/v1", model="m", api_key="k",
            max_retries=1, retry_backoff=0,
        )
    )
    assert results[0].labels == ("lfi.wrapper",)


def test_retries_are_bounded():
    client = StubClient([RuntimeError("a"), RuntimeError("b"), RuntimeError("c"), RuntimeError("d")])
    results = run(
        classify_batch(
            client, ["cmd one long enough here"],
            base_url="http://x/v1", model="m", api_key="k",
            max_retries=1, retry_backoff=0,
        )
    )
    assert not results[0].ok
    assert len(client.requests) == 2


def test_http_error_code_is_preserved():
    client = StubClient([StubResponse(521, text="origin down")])
    results = run(
        classify_batch(
            client, ["a command long enough to classify"],
            base_url="http://x/v1", model="m", api_key="k",
        )
    )
    assert results[0].error == "http_521"


def test_request_body_is_deterministic_and_bounded():
    client = StubClient([ok(["unknown"])])
    run(
        classify_batch(
            client, ["x" * 5000], base_url="http://x/v1", model="m", api_key="k"
        )
    )
    body = client.requests[0]["json"]
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["model"] == "m"
    # The payload is wrapped in markers, so the bound applies to the command
    # portion; the wrapper itself is a fixed, small overhead.
    content = body["messages"][1]["content"]
    assert "<<<COMMAND" in content and "COMMAND>>>" in content
    assert len(content) <= 1400


def test_json_mode_is_requested_by_default():
    # glm-5.3 ignores prose instructions but honours response_format; without
    # this the parse rate on this relay was 0%.
    client = StubClient([ok(["unknown"])])
    run(
        classify_batch(
            client, ["a command long enough"], base_url="http://x/v1", model="m", api_key="k"
        )
    )
    assert client.requests[0]["json"]["response_format"] == {"type": "json_object"}


def test_json_mode_can_be_disabled():
    client = StubClient([ok(["unknown"])])
    run(
        classify_batch(
            client, ["a command long enough"], base_url="http://x/v1", model="m",
            api_key="k", json_mode=False,
        )
    )
    assert "response_format" not in client.requests[0]["json"]


def test_token_budget_accounts_for_reasoning():
    # These models bill reasoning_content against max_tokens; a small budget
    # produced finish_reason=length with empty content.
    client = StubClient([ok(["unknown"])])
    run(
        classify_batch(
            client, ["a command long enough"], base_url="http://x/v1", model="m", api_key="k"
        )
    )
    assert client.requests[0]["json"]["max_tokens"] >= 600


def test_truncation_before_answer_is_its_own_error():
    client = StubClient(
        [StubResponse(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})]
    )
    results = run(
        classify_batch(
            client, ["a command long enough"], base_url="http://x/v1", model="m", api_key="k"
        )
    )
    assert results[0].error == "truncated_before_answer"


def test_base_url_slash_is_normalised():
    client = StubClient([ok(["unknown"])])
    run(
        classify_batch(
            client, ["a command long enough"], base_url="http://x/v1/", model="m", api_key="k"
        )
    )
    assert client.requests[0]["url"] == "http://x/v1/chat/completions"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_roundtrip(tmp_path: Path):
    cache = TechniqueCache(tmp_path / "c.jsonl", model="m")
    result = ClassificationResult("abc123", ("sqli.union",), 0.9, "why", "m")
    cache.append([result])
    loaded = cache.load()
    assert loaded["abc123"]["labels"] == ["sqli.union"]
    assert loaded["abc123"]["model"] == "m"
    assert loaded["abc123"]["prompt"] == PROMPT_VERSION


def test_cache_skips_results_without_hash(tmp_path: Path):
    cache = TechniqueCache(tmp_path / "c.jsonl", model="m")
    cache.append([ClassificationResult("", ("unknown",), 0.1, "", "m")])
    assert cache.load() == {}


def test_cache_keeps_last_verdict_for_a_hash(tmp_path: Path):
    # Append-only: re-classifying one command must not require editing history.
    path = tmp_path / "c.jsonl"
    cache = TechniqueCache(path, model="m")
    cache.append([ClassificationResult("h1", ("unknown",), 0.1, "", "m")])
    cache.append([ClassificationResult("h1", ("sqli.union",), 0.9, "", "m")])
    assert cache.load()["h1"]["labels"] == ["sqli.union"]


def test_cache_survives_a_corrupt_line(tmp_path: Path):
    path = tmp_path / "c.jsonl"
    path.write_text('{"h":"a","labels":["unknown"]}\n<<garbage>>\n', encoding="utf-8")
    loaded = TechniqueCache(path, model="m").load()
    assert list(loaded) == ["a"]


def test_cache_appends_newlines_correctly(tmp_path: Path):
    path = tmp_path / "c.jsonl"
    cache = TechniqueCache(path, model="m")
    cache.append([ClassificationResult("h1", ("unknown",), 0.1, "", "m")])
    cache.append([ClassificationResult("h2", ("unknown",), 0.1, "", "m")])
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    assert all(json.loads(line) for line in lines)


# ---------------------------------------------------------------------------
# Model selection (operator-configurable)
# ---------------------------------------------------------------------------


ROWS = [
    {"id": "id-glm", "name": "GLM", "base_url": "https://a/v1", "model_name": "glm-5.3"},
    {"id": "id-ds", "name": "deepseek", "base_url": "https://b", "model_name": "deepseek-v4"},
]


def test_resolve_model_by_name_case_insensitively():
    assert resolve_model_entry(ROWS, "glm")["id"] == "id-glm"


def test_resolve_model_by_id():
    assert resolve_model_entry(ROWS, "id-ds")["name"] == "deepseek"


def test_resolve_model_unknown_lists_available():
    with pytest.raises(ValueError) as excinfo:
        resolve_model_entry(ROWS, "nope")
    assert "GLM" in str(excinfo.value) and "deepseek" in str(excinfo.value)


def test_resolve_model_on_empty_registry_raises():
    with pytest.raises(ValueError):
        resolve_model_entry([], "glm")


def test_credentials_add_v1_when_missing():
    base, model, key = load_model_credentials(
        {"base_url": "https://a", "model_name": "m", "api_key": "k"}
    )
    assert base == "https://a/v1"
    assert model == "m" and key == "k"


def test_credentials_keep_existing_v1():
    base, _, _ = load_model_credentials(
        {"base_url": "https://a/v1/", "model_name": "m", "api_key": "k"}
    )
    assert base == "https://a/v1"


def test_credentials_reject_incomplete_config():
    with pytest.raises(ValueError):
        load_model_credentials({"base_url": "https://a", "model_name": "m", "api_key": ""})


# ---------------------------------------------------------------------------
# Concurrent appends
# ---------------------------------------------------------------------------


def test_two_writers_do_not_corrupt_the_cache(tmp_path: Path):
    """Two workers on one cache is the documented way to go faster.

    Measured 2026-11-04: one process tops out near 215 commands/minute (429
    above concurrency 16), so the throughput answer is a second process -- which
    puts two writers on one file.  Without the lock their partial lines
    interleave and every later reader loses the rows.
    """

    import threading

    path = tmp_path / "cache.jsonl"
    workers = 2
    per_worker = 40

    def worker(offset: int) -> None:
        cache = TechniqueCache(path, model="m")
        for i in range(per_worker):
            cache.append(
                [
                    ClassificationResult(
                        f"h{offset}-{i}", ("unknown",), 0.9, "w", "m"
                    )
                ]
            )

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    cache = TechniqueCache(path, model="m")
    entries = cache.load()
    assert len(entries) == workers * per_worker
    # Every line must still be valid JSON -- that is what interleaving broke.
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_append_is_a_single_write_per_batch(tmp_path: Path):
    path = tmp_path / "cache.jsonl"
    cache = TechniqueCache(path, model="m")
    cache.append(
        [
            ClassificationResult(f"h{i}", ("unknown",), 0.5, "w", "m")
            for i in range(5)
        ]
    )
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5


def test_empty_append_creates_no_file(tmp_path: Path):
    path = tmp_path / "cache.jsonl"
    TechniqueCache(path, model="m").append([])
    assert not path.exists()
