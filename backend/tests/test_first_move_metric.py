"""Pin the first-move A/B instrumentation and the AGENTS.md ordering contract.

Two things are tested here, both because they are easy to break silently:

``_EXPLOIT_SHAPES``  is a measuring instrument.  If it drifts, the A/B numbers
                     change meaning without anyone noticing, so each pattern
                     class that was actually needed to reach the current accuracy
                     gets a regression case.  The counter-examples matter more
                     than the positive ones: a false positive at step 2 makes the
                     metric look great while measuring nothing.

``workspace``        has a hard positional requirement: Workers open AGENTS.md
                     with ``cat | head -50``, ``cat``, ``head -60`` and five
                     other forms, and only six of the nine reach past the first
                     fifty lines.  ``FIRST_MOVE_DISCIPLINE`` is therefore only
                     useful while it stays in that window, and nothing else in
                     the codebase would notice if something were prepended.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from measure_first_exploit import analyse, classify_command  # noqa: E402


# ---------------------------------------------------------------------------
# Detector: positive cases, one per pattern class that was actually needed
# ---------------------------------------------------------------------------


def test_template_injection_payload_is_an_exploit():
    # The Confluence trace wrote this at step 51 and then abandoned it.
    assert classify_command('#set ($e="exp")\n#set ($a=$e.getClass())') == "exploit"


def test_running_a_purpose_built_script_counts():
    # Payload never appears on the command line: ``python3 exploit.py`` is the
    # whole signal.  Measured on 65d323f2 (CISCN Easyweb).
    assert classify_command("cd /home/kali/workspace && python3 exploit.py") == "exploit"


def test_credential_abuse_counts_as_exploitation():
    # 72d923d1 (OpenSSH CVE-2018-15473) got root with sshpass, not an HTTP payload.
    assert classify_command("sshpass -p vulhub ssh -p 25754 root@host") == "exploit"


def test_tooling_that_is_itself_the_attack_counts():
    assert classify_command("java -jar ysoserial-all.jar ysoserial.exploit.RMIRegistryExploit") == "exploit"
    assert classify_command("sqlmap -u http://t/?id=1 --batch") == "exploit"


def test_heredoc_body_payload_is_seen():
    # Spring CVE-2022-22947 wrote SpEL routes into exploit.sh.
    body = "cat > exploit.sh << 'EOF'\n#!/bin/bash\ncurl -d '{\"filters\":[...SpEL...]}'\nEOF"
    assert classify_command(body) == "exploit"


def test_path_traversal_through_a_parameter():
    assert classify_command("curl 'http://t/?param=/proc/self/cwd/flag.txt'") == "exploit"
    assert classify_command("curl 'http://t/?f=../../../../etc/passwd'") == "exploit"


def test_file_read_via_the_services_own_cli():
    # Jenkins CVE-2024-23897: the exploit is a CLI argument, not a payload string.
    assert classify_command("java -jar jenkins-cli.jar -http connect-node '@/etc/passwd'") == "exploit"


# ---------------------------------------------------------------------------
# Detector: negative cases.  These are the ones that made earlier drafts wrong.
# ---------------------------------------------------------------------------


def test_plain_shell_chaining_is_not_an_exploit():
    """The Worker orchestrates itself with ``&&`` and ``;`` constantly.

    An early draft matched ``; <command>`` and therefore scored ``ls pocs/ &&
    cat README`` (Confluence step 16) as an exploit at step 2, which made the
    whole metric meaningless.
    """

    assert classify_command("ls /home/kali/pocs/vulhub/confluence/ && cat README.md") == "other"
    assert classify_command("which blackboard.py; echo $DB") == "other"
    assert classify_command("cd /home/kali/workspace && curl -sS http://t/status -o /tmp/s") == "other"


def test_driving_the_installer_is_never_an_exploit():
    """Even when a payload fragment is present.

    The Confluence Run "prepared" a Velocity chain *while* polling the setup
    wizard, and driving that wizard was the actual mistake being measured.
    """

    assert classify_command("curl 'http://t/setup/setupstart.action' -o /tmp/s.html") == "installer"
    assert classify_command("curl -b /tmp/cj 'http://t/bootstrap/selectsetupstep.action'") == "installer"
    assert classify_command("curl http://t/setup/evallicense.action?x=$(id)") == "installer"


def test_endpoint_enumeration_is_not_an_exploit():
    assert classify_command("for p in /status /login.action /rest/api/content; do curl $B$p; done") == "other"
    assert classify_command("gobuster dir -u http://t -w /usr/share/wordlists/common.txt") == "other"


# ---------------------------------------------------------------------------
# analyse(): the shape of the reported numbers
# ---------------------------------------------------------------------------


def test_first_exploit_step_matches_a_hand_read_trace():
    """A miniature of the real Confluence sequence.

    Steps 1-3 probe and drive the installer; step 4 writes the Velocity payload;
    steps 5-6 wander.  The hand-read trace of ``5cb19393`` said: first exploit at
    the payload write, installer touched before it, a long gap afterwards.
    """

    steps = [
        "ls /home/kali/workspace",
        "curl 'http://t/setup/setupstart.action' -o /tmp/s.html",
        "curl 'http://t/bootstrap/selectsetupstep.action'",
        'cat > v.vm <<\'EOF\'\n#set ($e="exp")\nEOF',
        "curl 'http://t/setup/setupdbtype.action'",
        "gobuster dir -u http://t -w w.txt",
    ]
    row = analyse(steps)
    assert row["first_exploit_step"] == 4
    assert row["first_installer_step"] == 2
    assert row["installer_before_exploit"] is True
    assert row["exploit_steps"] == 1
    # Only one exploit, so there is no "gap to the next".
    assert row["gap_after_first_exploit"] is None


def test_gap_after_first_exploit_measures_abandonment():
    steps = [
        'cat > v.vm <<\'EOF\'\n#set ($e="exp")\nEOF',
        "a", "b", "c",
        "curl 'http://t/?f=../../etc/passwd'",
    ]
    row = analyse(steps)
    assert row["first_exploit_step"] == 1
    assert row["gap_after_first_exploit"] == 4


def test_run_with_no_exploit_reports_none_not_zero():
    """``None`` and ``0`` mean different things and must not be conflated.

    ``None`` = never attempted an exploit.  Averaging that as ``0`` would make a
    Run that never tried look like one that tried immediately.
    """

    row = analyse(["ls", "cat robots.txt", "curl http://t/"])
    assert row["first_exploit_step"] is None
    assert row["exploit_steps"] == 0
    assert row["installer_before_exploit"] is False


# ---------------------------------------------------------------------------
# AGENTS.md positional contract
# ---------------------------------------------------------------------------


def test_first_move_block_lands_inside_the_head_50_window():
    """The block is worthless if it drifts past line 50.

    Workers read ``AGENTS.md`` nine different ways (``cat | head -50``,
    ``cat``, ``cat | head -60``, ``cat | tail -50``, ``rg``, ...) and only six
    of the nine reach the top.  Recording the constraint as a test is the only
    thing that will notice a future edit prepending content above it.
    """

    from app.services.known_exploit import FIRST_MOVE_DISCIPLINE, RECON_DISCIPLINE
    from app.services.workspace import RUN_POLICY

    md = RUN_POLICY + FIRST_MOVE_DISCIPLINE + RECON_DISCIPLINE
    head50 = "\n".join(md.splitlines()[:50])

    assert "## Your first move" in head50
    # And it must come before the block it was written to precede.
    assert md.index("## Your first move") < md.index("## Before your first payload")


def test_first_move_block_only_says_what_the_data_supports():
    """The block must not re-grow bullets that were measured and deleted.

    Two bullets -- "the target's own install state is not your target" and "a
    dead end is a switch, not a retry" -- were removed on 2026-11-05 after being
    checked against 277 Runs: the installer behaviour occurred in 3.6% of Runs,
    and the step-count threshold cannot separate real stalling from the noise
    (within-challenge spread of first-exploit step: median 12, max 60).

    This test is the tripwire.  Adding a constraint that has not been measured is
    how a prompt turns into filler that costs ``head -50`` space and teaches the
    Worker nothing.
    """

    from app.services.known_exploit import FIRST_MOVE_DISCIPLINE as block

    lowered = block.casefold()
    # The one claim that survived measurement.
    assert "hypothesis" in lowered
    # Deleted: 3.6% coverage, generalised from a single outlier trace.
    assert "install" not in lowered
    assert "setup wizard" not in lowered
    # Deleted: threshold not separable from noise.
    assert "dead end" not in lowered



def test_flag_off_removes_the_block(monkeypatch):
    """The A/B switch has to actually switch."""

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "first_move_discipline", False)
    from app.services.known_exploit import FIRST_MOVE_DISCIPLINE, RECON_DISCIPLINE
    from app.services.workspace import RUN_POLICY

    md = RUN_POLICY + (FIRST_MOVE_DISCIPLINE if settings.first_move_discipline else "") + RECON_DISCIPLINE
    assert "## Your first move" not in md
    # The older block must survive: this experiment must not regress the
    # discipline that already earned its place.
    assert "## Before your first payload" in md
