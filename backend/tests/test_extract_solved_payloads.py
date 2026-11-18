"""The solved-Run payload extractor must never leak, and must never invent.

Two properties matter and both fail silently:

leakage    a candidate file containing a real target host, a real flag, or a
           third party's IP would be a genuine disclosure.  A draft that keyed
           the host pattern on ``https?://…dasctf…`` missed
           ``root@direct-ctf2.dasctf.com`` in an ssh command -- caught only
           because the candidate was read by eye.  These tests are the automated
           version of that read.

replayability
           a queue full of ``--help`` and ``which x`` entries trains a reviewer
           to rubber-stamp, which defeats the point of a review queue.  Nine of
           the first 43 candidates were exactly that.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import re  # noqa: E402

from extract_solved_payloads import (  # noqa: E402
    _NOT_A_PAYLOAD,
    _guess_technique,
    _scrub,
)


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------


def test_bare_hostname_without_a_scheme_is_scrubbed():
    """The ssh case: ``root@direct-ctf2.dasctf.com`` has no ``http://`` prefix."""

    out, found = _scrub("sshpass -p v ssh -p 25754 root@direct-ctf2.dasctf.com")
    assert "dasctf.com" not in out
    assert "{{target_url}}" in out
    assert "target_url" in found


def test_http_and_https_hosts_are_both_scrubbed():
    for raw in (
        "curl http://abc123.http-ctf2.dasctf.com:80/x",
        "curl https://direct-ctf2.dasctf.com:28889/gremlin",
    ):
        out, _ = _scrub(raw)
        assert "dasctf.com" not in out, raw
        assert "{{target_url}}" in out


def test_private_networks_are_scrubbed():
    out, _ = _scrub("curl http://192.168.236.1:28221/")
    assert "192.168." not in out
    assert "{{host}}" in out


def test_a_third_party_public_ip_is_scrubbed():
    """Seen in an ActiveMQ candidate: an unrelated public IP in the command."""

    out, _ = _scrub("python3 poc.py 117.21.200.176 26334")
    assert "117.21" not in out
    assert "{{ip}}" in out


def test_flags_of_both_shapes_are_scrubbed():
    for raw in ("echo CTF2{54691f9c-abcd-1234} > f", "echo flag{anything_at_all} > f"):
        out, _ = _scrub(raw)
        assert "{" not in out or "{{flag}}" in out, raw
        assert "CTF2{" not in out and "flag{" not in out


def test_scrub_reports_what_it_replaced():
    """The reviewer needs to know which placeholders to fill in."""

    _, found = _scrub("curl http://direct-ctf2.dasctf.com:28889/ -d CTF2{aaaaaaaa-bbbb}")
    assert "target_url" in found
    assert "flag" in found


# ---------------------------------------------------------------------------
# Not-a-payload rejection
# ---------------------------------------------------------------------------


def test_usage_lookups_are_not_payloads():
    for raw in (
        "java -cp ysoserial-all.jar ysoserial.GeneratePayload 2>&1 | head -40",
        "which redis-cli",
        "redis-cli --help",
        "ls -la /home/kali/pocs",
        "grep -c foo bar.txt",
    ):
        assert _NOT_A_PAYLOAD.search(raw), raw


def test_a_real_payload_is_not_rejected():
    for raw in (
        "curl -sS 'http://t/geneSign?param=/proc/self/cwd/flag.txt'",
        "java -cp ysoserial-all.jar ysoserial.exploit.RMIRegistryExploit host 27605 BeanShell1 'id'",
        "sshpass -p vulhub ssh -p 25754 root@host 'id'",
    ):
        assert not _NOT_A_PAYLOAD.search(raw), raw


# ---------------------------------------------------------------------------
# Technique labelling
# ---------------------------------------------------------------------------


def test_payload_shape_beats_the_challenge_title():
    """A box called "SSRF Me" can be solved by forging a signature.

    Filing it under ``ssrf`` would then never retrieve the thing that worked,
    because retrieval is by technique.
    """

    technique = _guess_technique(
        'BASE="http://t"; SIGN=$(curl -s "$BASE/geneSign?param=/x"); '
        'curl -b "action=readscan; sign=$SIGN" "$BASE/De1ta?param=/x"',
        "[De1CTF 2019]SSRF Me",
    )
    # No ssrf primitive (no 169.254, no loopback) in the payload: keep it honest.
    assert technique != "ssrf" or "169.254.169.254" in "x"


def test_known_shapes_get_their_labels():
    cases = [
        ("sshpass -p v ssh root@h 'id'", "x", "credential-abuse"),
        ("java -cp y.jar ysoserial.exploit.RMIRegistryExploit h 1 X 'id'", "x", "deserialization"),
        ("curl -d '{\"gremlin\":\"1+1\"}' http://t/gremlin", "x", "graph-query-injection"),
        ("cat > p.jpg <<'EOF'\n%!PS-Adobe-3.0 EPSF\nEOF", "x", "image-parser-rce"),
        ("curl 'http://t/?f=../../../../etc/passwd'", "x", "path-traversal"),
        ("curl 'http://t/?page=php://filter/convert.base64-encode/resource=x'", "x", "lfi"),
        ("curl -d \"q=-1 union select 1,2,3\" http://t/", "x", "sqli"),
    ]
    for command, name, expected in cases:
        assert _guess_technique(command, name) == expected, command


def test_unrecognised_payload_says_unknown_rather_than_guessing():
    assert _guess_technique("echo hello world > /tmp/a", "Some Challenge") == "unknown"


def test_title_is_the_fallback_only():
    """With no payload signal, the title decides -- but only then."""

    assert _guess_technique("curl http://t/", "[极客大挑战]Upload") == "file-upload"


# ---------------------------------------------------------------------------
# The candidate file, if it exists, must be clean
# ---------------------------------------------------------------------------


def test_committed_candidate_file_contains_no_live_secrets():
    """Guards the artifact, not just the function.

    Runs against the real output so a future change to the scrubber that passes
    unit tests but leaks on real data still fails here.
    """

    import json

    path = ROOT / "data" / "knowledge" / "candidates" / "payload-candidates.jsonl"
    if not path.exists():
        return
    blob = path.read_text(encoding="utf-8")
    for pattern, label in (
        (r"dasctf\.com", "真实靶机主机名"),
        (r"CTF2\{[0-9a-f-]{8,}", "真实 flag"),
        (r"\b192\.168\.", "内网地址"),
        (r"\bsk-[A-Za-z0-9]{16,}", "API key"),
    ):
        assert not re.search(pattern, blob), f"候选文件泄漏{label}"
    # Every line must still parse.
    for line in blob.splitlines():
        json.loads(line)
