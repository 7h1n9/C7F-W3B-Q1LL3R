"""The candidate audit must reject what actually went wrong, not what might.

Every case below is taken from a specific entry in the first extraction batch
that a hand review found to be unusable.  The rules started naive and were
wrong three times, so the regressions are pinned individually:

1. The plaintext flag scan passed a file containing a base64 blob that decodes
   to a real ``CTF2{...}`` flag.
2. Measuring the *template* length missed 25 entries whose raw command had been
   clipped at 300 chars upstream, because redaction made the template shorter.
3. ``cat > f2.jpg <<EOF`` creates a payload and sends nothing.
4. ``curl shell.php?cmd=cat /flag`` is the step *after* the flag was reachable.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "audit_payload_candidates", ROOT / "scripts" / "audit_payload_candidates.py"
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def entry(template: str, **extra) -> dict:
    base = {
        "kind": "poc",
        "title": "t",
        "technique": "unknown",
        "payload_template": template,
        "provenance": {"run_id": "r", "decisive_sequence": 1},
    }
    base.update(extra)
    return base


# --- flag leakage ----------------------------------------------------------


def test_plaintext_flag_rejects():
    verdict, reasons = audit.judge(entry("curl http://x/ | grep CTF2{abc-123456}"), None)
    assert verdict == "reject"
    assert "flag" in reasons[0]


def test_base64_encoded_flag_rejects():
    """The real leak: ``echo '<b64>' | base64 -d`` carries a live flag.

    A plaintext-only scan reported this file clean, which is how a real flag
    reached an on-disk corpus.  The check must decode.
    """

    payload = "<?php\n//CTF2{9572be45-f073-4f6b-a66b-b5a9cd4cf5c9}\n"
    blob = base64.b64encode(payload.encode()).decode()
    verdict, reasons = audit.judge(entry(f"echo '{blob}' | base64 -d; curl http://x/"), None)
    assert verdict == "reject"
    assert "flag" in reasons[0]


def test_clean_base64_is_not_a_false_positive():
    blob = base64.b64encode(b"<?php echo 'hello world, nothing secret here'; ?>").decode()
    verdict, _ = audit.judge(entry(f"echo '{blob}' | base64 -d"), None)
    assert verdict != "reject"


# --- truncation ------------------------------------------------------------


def test_raw_length_at_clip_rejects_even_when_template_is_short():
    """A 300-char raw command is cut; the template can be shorter after scrubbing."""

    verdict, reasons = audit.judge(entry("curl -s http://x/ -d 'a=1'"), raw_len=300)
    assert verdict == "reject"
    assert "截断" in reasons[0]


def test_unclosed_heredoc_rejects_regardless_of_length():
    """``[CISCN]Web4`` is only 257 chars yet stops at ``class RCE:``."""

    template = "python3 << 'EOF'\nimport pickle\n\nclass RCE:\n"
    verdict, reasons = audit.judge(entry(template), raw_len=len(template))
    assert verdict == "reject"
    assert "heredoc" in reasons[0]


def test_closed_heredoc_passes():
    template = "python3 << 'EOF'\nprint(1)\nEOF\n"
    verdict, _ = audit.judge(entry(template), raw_len=len(template))
    assert verdict == "accept"


# --- "decisive" is not "exploitation" --------------------------------------


def test_retrieval_from_planted_shell_rejects():
    verdict, reasons = audit.judge(entry('curl -s "http://x/shell.php%0a?cmd=env"'), None)
    assert verdict == "reject"
    assert "回读" in reasons[0]


def test_create_only_payload_needs_repair():
    template = "cat > f2.jpg <<'PYEOF'\n%!PS\nshowpage\nPYEOF\n"
    verdict, reasons = audit.judge(entry(template), raw_len=len(template))
    assert verdict == "repair"
    assert "未发送" in reasons[0]


@pytest.mark.parametrize(
    "template,label",
    [
        ("./q.sh 'env | grep flag'", "前序脚本"),
        ("cat scratch/tok.txt", "前序文件"),
        ("curl -b cj -X POST http://x/", "会话"),
        ("curl 'http://x/?file=source.php?/../../ffffllllaaaagggg'", "题解专用路径"),
    ],
)
def test_prerequisite_dependencies_need_repair(template, label):
    verdict, reasons = audit.judge(entry(template), None)
    assert verdict == "repair"
    assert any(label in r for r in reasons)


def test_redacted_credential_needs_repair():
    """``postgres:[redacted]@host`` cannot be filled in -- it is unplayable."""

    template = 'psql "postgresql://postgres:[redacted]@host/db" -c "COPY t FROM PROGRAM \'env\'"'
    verdict, reasons = audit.judge(entry(template), None)
    assert verdict == "repair"
    assert "脱敏" in reasons[0]


# --- the one that survives -------------------------------------------------


def test_self_contained_exploit_accepts():
    template = (
        'curl -s -H "Accept: ../../../../../../../../proc/self/environ{{" '
        '{{target_url}}/robots 2>&1 | strings | grep -iE "flag"'
    )
    verdict, reasons = audit.judge(entry(template), raw_len=len(template))
    assert verdict == "accept", reasons
